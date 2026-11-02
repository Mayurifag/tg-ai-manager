import asyncio
import json
import os
import sys

# Alembic Imports
from alembic import command
from alembic.config import Config as AlembicConfig
from jinja2 import select_autoescape
from src.web.types import TypedQuart

from src.adapters.telegram import TelethonAdapter
from src.adapters.valkey_repo import ValkeyActionRepository, ValkeyEventRepository
from src.application.interactors import ChatInteractor
from src.config import get_settings
from src.container import bind_adapter_events
from src.infrastructure.event_bus import EventBus
from src.infrastructure.html import html_to_text
from src.infrastructure.logging import configure_logging, get_logger
from src.infrastructure.security import SecretStorageError
from src.domain.errors import LogUnavailable
from src.infrastructure.tasks import BackgroundTasks
from src.jinja_filters import file_mtime_filter
from src.rules.service import RuleService
from src.rules.sqlite_repo import SqliteRuleRepository
from src.rules.sync import sync_rules_from_remote
from src.users.sqlite_repo import SqliteUserRepository
from src.infrastructure.maintenance import (
    job_background_maintenance,
    job_telegram_recovery,
)
from src.web.routes import register_routes
from src.web.serializers import json_serializer
from src.web.sse import EventBroadcaster

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
    except SecretStorageError:
        # Do not silently start as a new account when persisted credentials are unreadable.
        raise
    except Exception as e:
        # A database failure is not evidence that no saved account exists.
        logger.error("session_load_failed", category=type(e).__name__)
        raise

    return TelethonAdapter(
        session_string=session_string,
        api_id=settings.TG_API_ID,
        api_hash=settings.TG_API_HASH,
    )


def create_app() -> TypedQuart:
    # Configure structured logging
    configure_logging()

    # Set the project root explicitly
    root_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))

    app = TypedQuart(__name__, root_path=root_path, template_folder="src/templates")
    app.login_lock = asyncio.Lock()
    app.sse = EventBroadcaster()
    app.shutdown_event = asyncio.Event()
    # Quart's default recognizes .html, but not the project's .html.j2 files.
    app.jinja_env.autoescape = select_autoescape(
        enabled_extensions=("html", "htm", "xml", "xhtml", "svg", "html.j2")
    )

    # Register filters
    file_mtime_filter(app)
    app.jinja_env.filters["plain_text"] = html_to_text

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
        app.shutdown_event.clear()

        # 3. Create repositories
        settings = get_settings()
        action_repo = ValkeyActionRepository(settings.VALKEY_URL)
        event_repo = ValkeyEventRepository(settings.VALKEY_URL)
        user_repo = SqliteUserRepository(db_path=settings.DB_PATH)

        # 4. Create Telegram adapter
        tg_adapter = await _build_tg_adapter(settings, user_repo)

        # 5. Create services
        rule_repo = SqliteRuleRepository(db_path=settings.DB_PATH)
        rule_service = RuleService(rule_repo, action_repo, tg_adapter, user_repo)
        interactor = ChatInteractor(tg_adapter, action_repo, event_repo)

        # 6. Attach services to app for app-scoped access
        app.tg_adapter = tg_adapter
        app.action_repo = action_repo
        app.event_repo = event_repo
        app.user_repo = user_repo
        app.rule_service = rule_service
        app.chat_interactor = interactor
        app.managed_tasks = BackgroundTasks(logger)

        # Present arrival data before optional AI and persistence work. A queued
        # action is not a completed action and is reported separately.
        bus = EventBus()
        bus.subscribe(interactor.handle_event)

        async def _sse_broadcast(event):
            async with app.app_context():
                await app.sse.broadcast(event)

        bus.subscribe(_sse_broadcast)

        async def _persist_event(event):
            await asyncio.wait_for(event_repo.add_event(event), timeout=2.0)

        # Persist the same arrival state presented to viewers, before any
        # asynchronous automation can complete and mutate its old event.
        bus.subscribe(_persist_event)
        bus.subscribe(rule_service.handle_new_message_event)
        app.event_bus = bus

        # Register before connecting so initial updates cannot miss the bus.
        bind_adapter_events(app, tg_adapter)
        await interactor.initialize()

        # Sync only after Telegram has identified the authenticated account.
        if settings.RULES_SYNC_URL:
            await sync_rules_from_remote(
                settings.RULES_SYNC_URL,
                rule_repo,
                user_repo,
                expected_account_id=tg_adapter._self_id,
                token=settings.RULES_SYNC_TOKEN,
            )
        else:
            logger.info("rules_sync_skipped", reason="RULES_SYNC_URL not set")

        # 9. Background tasks
        app.managed_tasks.create(rule_service.run_startup_scan(), "startup_scan")
        app.managed_tasks.create(
            interactor.run_cache_warmer(app.shutdown_event), "chat_cache_warmer"
        )
        app.managed_tasks.create(
            job_telegram_recovery(rule_service, user_repo, app.shutdown_event),
            "telegram_recovery",
        )
        app.managed_tasks.create(
            job_background_maintenance(
                action_repo=action_repo,
                event_repo=event_repo,
                interactor=interactor,
                user_repo=user_repo,
                shutdown_event=app.shutdown_event,
            ),
            "maintenance",
        )

    @app.after_serving
    async def shutdown():
        logger.info("application_shutdown")
        app.shutdown_event.set()
        app.tg_adapter._retired = True
        app.sse.close()
        await app.managed_tasks.shutdown(timeout=3.0)
        await app.rule_service.retire_account()

        # Allow SSE generators to exit gracefully
        await asyncio.sleep(0.1)

        interactor = app.chat_interactor
        try:
            await asyncio.wait_for(interactor.shutdown(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.error("telegram_shutdown_timeout")
        except Exception as e:
            logger.error("shutdown_error", error=str(e))

        for repo in (app.action_repo, app.event_repo):
            close = getattr(repo, "close", None)
            if close:
                await close()

    @app.context_processor
    async def inject_globals():
        """
        Injects recent events and current user into the template context.
        """
        from typing import cast
        from quart import current_app

        typed_app = cast(TypedQuart, current_app._get_current_object())  # noqa: SLF001
        interactor = typed_app.chat_interactor
        user_repo = typed_app.user_repo

        user = await user_repo.get_user(1)
        events = []
        events_unavailable = False
        if user and user.debug_mode:
            try:
                events = await asyncio.wait_for(
                    interactor.get_recent_events(), timeout=2.0
                )
            except (LogUnavailable, asyncio.TimeoutError):
                events_unavailable = True

        return {
            "recent_events": events,
            "recent_events_unavailable": events_unavailable,
            "current_user": user,
        }

    return app
