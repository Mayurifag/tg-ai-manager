import asyncio
import json
import os
import sys

# Alembic Imports
from alembic import command
from alembic.config import Config as AlembicConfig
from quart import Quart

from src.adapters.telegram import TelethonAdapter
from src.adapters.valkey_repo import ValkeyActionRepository, ValkeyEventRepository
from src.application.interactors import ChatInteractor
from src.config import get_settings
from src.infrastructure.event_bus import EventBus
from src.infrastructure.logging import configure_logging, get_logger
from src.jinja_filters import file_mtime_filter
from src.rules.service import RuleService
from src.rules.sqlite_repo import SqliteRuleRepository
from src.rules.sync import sync_rules_from_remote
from src.users.sqlite_repo import SqliteUserRepository
from src.infrastructure.maintenance import job_background_maintenance
from src.web.routes import register_routes
from src.web.serializers import json_serializer
from src.web.sse import broadcast_event, connected_queues, shutdown_event

logger = get_logger(__name__)


def run_migrations(root_path: str):
    """Executes Alembic migrations programmatically."""
    try:
        logger.info("running_migrations")
        alembic_ini_path = os.path.join(root_path, "alembic.ini")
        alembic_cfg = AlembicConfig(alembic_ini_path)

        script_location = os.path.join(root_path, "migrations")
        alembic_cfg.set_main_option("script_location", script_location)

        command.upgrade(alembic_cfg, "head")
        logger.info("migrations_completed")
    except Exception as e:
        logger.error("migrations_failed", error=str(e))
        sys.exit(1)


async def _build_tg_adapter(settings, user_repo) -> TelethonAdapter:
    """Create a TelethonAdapter, loading the session string from the DB if present."""
    session_string = None
    try:
        user = await user_repo.get_user(1)
        if user:
            session_string = user.session_string
    except Exception as e:
        logger.warning("session_load_failed", error=str(e))

    return TelethonAdapter(
        session_string=session_string,
        api_id=settings.TG_API_ID,
        api_hash=settings.TG_API_HASH,
    )


def create_app() -> Quart:
    # Configure structured logging
    configure_logging()

    # Set the project root explicitly
    root_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))

    app = Quart(__name__, root_path=root_path, template_folder="src/templates")

    # Register filters
    file_mtime_filter(app)

    @app.template_filter("to_json")
    def to_json_filter(obj):
        return json.dumps(obj, default=json_serializer, indent=2)

    # Register routes
    register_routes(app)

    @app.before_serving
    async def startup():
        logger.info("application_startup")

        # 1. Run Database Migrations
        run_migrations(root_path)

        # 2. Reset shutdown event
        shutdown_event.clear()

        # 3. Create repositories
        settings = get_settings()
        action_repo = ValkeyActionRepository(settings.VALKEY_URL)
        event_repo = ValkeyEventRepository(settings.VALKEY_URL)
        user_repo = SqliteUserRepository(db_path=settings.DB_PATH)

        # 3a. Sync rules from remote production instance (if configured)
        rule_repo_for_sync = SqliteRuleRepository(db_path=settings.DB_PATH)
        if settings.RULES_SYNC_URL:
            await sync_rules_from_remote(
                url=settings.RULES_SYNC_URL,
                rule_repo=rule_repo_for_sync,
                user_repo=user_repo,
            )
        else:
            logger.info("rules_sync_skipped", reason="RULES_SYNC_URL not set")

        # 4. Create Telegram adapter
        tg_adapter = await _build_tg_adapter(settings, user_repo)

        # 5. Create services
        rule_repo = SqliteRuleRepository(db_path=settings.DB_PATH)
        rule_service = RuleService(rule_repo, action_repo, tg_adapter, user_repo)
        interactor = ChatInteractor(tg_adapter, action_repo, event_repo)

        # 6. Attach services to app for app-scoped access
        app.tg_adapter = tg_adapter  # type: ignore[attr-defined]
        app.action_repo = action_repo  # type: ignore[attr-defined]
        app.event_repo = event_repo  # type: ignore[attr-defined]
        app.user_repo = user_repo  # type: ignore[attr-defined]
        app.rule_service = rule_service  # type: ignore[attr-defined]
        app.chat_interactor = interactor  # type: ignore[attr-defined]

        # 7. Create event bus and register subscribers in order:
        #    - event_repo first (persistence)
        #    - rule_service second (sets is_read before SSE renders)
        #    - SSE broadcast last
        bus = EventBus()
        bus.subscribe(event_repo.add_event)
        bus.subscribe(rule_service.handle_new_message_event)

        async def _sse_broadcast(event):
            async with app.app_context():
                await broadcast_event(event)

        bus.subscribe(_sse_broadcast)
        app.event_bus = bus  # type: ignore[attr-defined]

        # 8. Connect adapter and wire event bus as its sole listener
        await interactor.initialize()
        tg_adapter.add_event_listener(bus.dispatch)

        # 9. Background tasks
        asyncio.create_task(rule_service.run_startup_scan())
        asyncio.create_task(
            job_background_maintenance(
                action_repo=action_repo,
                event_repo=event_repo,
                interactor=interactor,
                user_repo=user_repo,
                shutdown_event=shutdown_event,
            )
        )

    @app.after_serving
    async def shutdown():
        logger.info("application_shutdown")
        shutdown_event.set()

        # Allow SSE generators to exit gracefully
        await asyncio.sleep(0.1)

        interactor = app.chat_interactor  # type: ignore[attr-defined]
        try:
            await asyncio.wait_for(interactor.shutdown(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.error("telegram_shutdown_timeout")
        except Exception as e:
            logger.error("shutdown_error", error=str(e))

        connected_queues.clear()

    @app.context_processor
    async def inject_globals():
        """
        Injects recent events and current user into the template context.
        """
        from quart import current_app

        interactor = current_app.chat_interactor  # type: ignore[attr-defined]
        user_repo = current_app.user_repo  # type: ignore[attr-defined]

        events = await interactor.get_recent_events()
        user = await user_repo.get_user(1)

        return {"recent_events": events, "current_user": user}

    return app
