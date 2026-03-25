import asyncio
import json
import os
import sqlite3
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
from src.infrastructure.security import CryptoManager
from src.jinja_filters import file_mtime_filter
from src.rules.service import RuleService
from src.rules.sqlite_repo import SqliteRuleRepository
from src.users.sqlite_repo import SqliteUserRepository
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


def _build_tg_adapter(settings) -> TelethonAdapter:
    """Create a TelethonAdapter, loading the session string from the DB if present."""
    crypto = CryptoManager()
    session_string = None
    try:
        with sqlite3.connect(settings.DB_PATH) as conn:
            cur = conn.execute("SELECT session_string FROM users WHERE id = 1")
            row = cur.fetchone()
            if row and row[0]:
                session_string = crypto.decrypt(row[0])
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

        # 4. Create Telegram adapter
        tg_adapter = _build_tg_adapter(settings)

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
        asyncio.create_task(job_background_maintenance())

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


async def job_background_maintenance():
    """Background task to clean old logs, enforce cache limits, and refresh premium status."""
    from quart import current_app

    action_repo = current_app.action_repo  # type: ignore[attr-defined]
    event_repo = current_app.event_repo  # type: ignore[attr-defined]
    interactor = current_app.chat_interactor  # type: ignore[attr-defined]
    user_repo = current_app.user_repo  # type: ignore[attr-defined]

    logger.info("maintenance_job_started")
    while not shutdown_event.is_set():
        try:
            await action_repo.cleanup_expired()
            await event_repo.cleanup_expired()

            user = await user_repo.get_user(1)
            if user and user.is_authenticated():
                await interactor.run_storage_maintenance()

                is_premium = await interactor.get_self_premium_status()
                if user.is_premium != is_premium:
                    user.is_premium = is_premium
                    await user_repo.save_user(user)
                    logger.info("premium_status_updated", status=is_premium)
            else:
                logger.info("maintenance_skipped_no_authenticated_user")

        except Exception as e:
            logger.error("maintenance_job_error", error=str(e))

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass
