import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack

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
from src.infrastructure.logging import (
    configure_logging,
    configure_web_logger,
    get_logger,
)
from src.infrastructure.security import SecretStorageError
from src.domain.errors import AccountChanged, LogUnavailable
from src.infrastructure.tasks import (
    BackgroundTasks,
    raise_if_task_stopped,
    run_with_task_deadline,
)
from src.jinja_filters import file_mtime_filter
from src.rules.service import RuleService
from src.rules.sqlite_repo import SqliteRuleRepository
from src.users.sqlite_repo import SqliteUserRepository
from src.infrastructure.maintenance import (
    job_background_maintenance,
    job_telegram_recovery,
)
from src.web.routes import register_routes
from src.web.serializers import json_serializer
from src.web.events import EventBroadcaster

logger = get_logger(__name__)
# shortcut: allow three seconds per cooperative resource close; revisit if
# normal cleanup routinely takes longer.
RESOURCE_CLOSE_TIMEOUT = 3.0
TEMPLATE_EVENT_HISTORY_TIMEOUT = 2.0
EVENT_PERSISTENCE_TIMEOUT = 2.0


async def _cleanup_resource(
    component: str,
    close: Callable[[], Awaitable[None]],
    *,
    timeout: float = RESOURCE_CLOSE_TIMEOUT,
) -> None:
    def log_error(error: Exception) -> None:
        logger.error(
            "resource_cleanup_failed",
            component=component,
            category=type(error).__name__,
        )

    async def close_resource() -> None:
        try:
            async with asyncio.timeout(timeout):
                await close()
        except Exception as error:
            log_error(error)

    try:
        # The resource can suppress and clear its own cancellation. Preserve
        # the owner's cancellation while still joining the close before the
        # next resource is unwound.
        await run_with_task_deadline(
            close_resource,
            None,
            name=f"cleanup_{component}",
            allow_pending_cancellation=True,
        )
    except Exception as error:
        log_error(error)


def run_migrations(root_path: str):
    """Executes Alembic migrations programmatically."""
    try:
        logger.info("running_migrations")
        alembic_ini_path = os.path.join(root_path, "alembic.ini")
        alembic_cfg = AlembicConfig(alembic_ini_path)
        # The application already owns logging; Alembic must not disable its
        # loggers or replace the server's existing handlers.
        alembic_cfg.attributes["configure_logging"] = False

        script_location = os.path.join(root_path, "migrations")
        alembic_cfg.set_main_option("script_location", script_location)

        command.upgrade(alembic_cfg, "head")
        logger.info("migrations_completed")
    except Exception as e:
        logger.error("migrations_failed", category=type(e).__name__)
        sys.exit(1)


async def _build_tg_adapter(settings, user_repo):
    """Create a TelethonAdapter, loading the session string from the DB if present."""
    try:
        user = await run_with_task_deadline(
            lambda: user_repo.get_user(1), None, name="startup_saved_session"
        )
    except SecretStorageError:
        # Do not silently start as a new account when persisted credentials are unreadable.
        raise
    except Exception as e:
        # A database failure is not evidence that no saved account exists.
        logger.error("session_load_failed", category=type(e).__name__)
        raise

    return (
        TelethonAdapter(
            session_string=user.session_string if user else None,
            api_id=settings.TG_API_ID,
            api_hash=settings.TG_API_HASH,
            saved_account_id=user.telegram_id if user else None,
        ),
        user,
    )


def create_app() -> TypedQuart:
    # Configure structured logging
    configure_logging()

    # Set the project root explicitly
    root_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))

    app = TypedQuart(__name__, root_path=root_path, template_folder="src/templates")
    configure_web_logger(app.logger)
    app.login_lock = asyncio.Lock()
    app._account_transition_revision = 0
    app._account_transition_depth = 0
    app._lifecycle_lock = asyncio.Lock()
    app._startup_owner = None
    app._startup_complete = False
    app._startup_unwinding = False
    app._shutdown_started = False
    app._shutdown_inflight = None
    app.events = EventBroadcaster()
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

    async def start_resources():
        logger.info("application_startup")

        def ensure_startup_active() -> None:
            raise_if_task_stopped()
            if app._shutdown_started or app.shutdown_event.is_set():
                raise RuntimeError("Application is shutting down")

        ensure_startup_active()
        # 1. Run Database Migrations
        run_migrations(root_path)

        settings = get_settings()
        tg_adapter = None
        async with AsyncExitStack() as cleanup:
            try:
                action_repo = ValkeyActionRepository(settings.VALKEY_URL)
                cleanup.push_async_callback(
                    _cleanup_resource, "action_repo", action_repo.close
                )
                event_repo = ValkeyEventRepository(settings.VALKEY_URL)
                cleanup.push_async_callback(
                    _cleanup_resource, "event_repo", event_repo.close
                )
                user_repo = SqliteUserRepository(db_path=settings.DB_PATH)

                tg_adapter, saved_user = await _build_tg_adapter(settings, user_repo)
                cleanup.push_async_callback(
                    _cleanup_resource, "telegram", tg_adapter.disconnect
                )
                ensure_startup_active()

                rule_repo = SqliteRuleRepository(db_path=settings.DB_PATH)
                rule_service = RuleService(
                    rule_repo, action_repo, tg_adapter, user_repo
                )
                cleanup.push_async_callback(
                    _cleanup_resource,
                    "ai_clients",
                    rule_service.retire_account,
                    timeout=4,
                )
                interactor = ChatInteractor(tg_adapter, action_repo, event_repo)

                app.tg_adapter = tg_adapter
                app.action_repo = action_repo
                app.event_repo = event_repo
                app.user_repo = user_repo
                app.rule_service = rule_service
                app.chat_interactor = interactor
                app.managed_tasks = BackgroundTasks(logger)
                cleanup.push_async_callback(
                    _cleanup_resource,
                    "background_tasks",
                    app.managed_tasks.shutdown,
                    timeout=4,
                )

                # Present arrival data before optional AI and persistence work.
                bus = EventBus()
                bus.subscribe(interactor.handle_event)

                async def _broadcast(event):
                    async with app.app_context():
                        await app.events.broadcast(event)

                bus.subscribe(_broadcast)

                async def _persist_event(event):
                    # These raw updates refresh views without creating new
                    # retained history for profile changes or remote reads.
                    if event.type in {"profile_updated", "chat_list_changed"} or (
                        event.type == "read" and event.read_origin == "remote"
                    ):
                        return
                    deadline = (
                        asyncio.get_running_loop().time() + EVENT_PERSISTENCE_TIMEOUT
                    )
                    await run_with_task_deadline(
                        lambda: event_repo.add_event(event),
                        deadline,
                        name="event_history_write",
                    )

                # Persist arrival state before automation can mutate the event.
                bus.subscribe(_persist_event)
                bus.subscribe(rule_service.handle_new_message_event)
                app.event_bus = bus

                # Register before connecting so initial updates cannot miss the bus.
                bind_adapter_events(app, tg_adapter)
                await run_with_task_deadline(
                    interactor.initialize, None, name="startup_telegram_connect"
                )
                ensure_startup_active()
                if (
                    saved_user
                    and saved_user.telegram_id is None
                    and tg_adapter.is_connected()
                ):
                    await user_repo.update_fields(
                        saved_user.id,
                        {"telegram_id": tg_adapter._self_id},
                        expected_session=saved_user.session_string,
                    )

                ensure_startup_active()
                app.managed_tasks.create(
                    rule_service.run_startup_scan(tg_adapter), "startup_scan"
                )
                app.managed_tasks.create(
                    interactor.run_prefetch_workers(), "chat_prefetch"
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
            except BaseException:
                # Shutdown must not interrupt a close already unwinding startup.
                app._startup_unwinding = True
                app.shutdown_event.set()
                if tg_adapter is not None:
                    tg_adapter.retire()
                raise
            cleanup.pop_all()
            app._startup_complete = True

    @app.before_serving
    async def startup():
        try:
            async with app._lifecycle_lock:
                await start_resources()
        finally:
            app._startup_unwinding = False

    async def close_started_resources():
        # Unwind every owned resource even if shutdown is cancelled partway
        # through. The original cancellation still propagates after cleanup.
        async with AsyncExitStack() as cleanup:
            for component, repo in (
                ("event_repo", app.event_repo),
                ("action_repo", app.action_repo),
            ):
                close = getattr(repo, "close", None)
                if close:
                    cleanup.push_async_callback(_cleanup_resource, component, close)
            cleanup.push_async_callback(
                _cleanup_resource, "telegram", app.chat_interactor.shutdown
            )
            # Allow SSE generators to exit gracefully before disconnecting.
            cleanup.push_async_callback(asyncio.sleep, 0.1)
            cleanup.push_async_callback(
                _cleanup_resource,
                "ai_clients",
                app.rule_service.retire_account,
                timeout=4.0,
            )
            cleanup.push_async_callback(
                _cleanup_resource,
                "background_tasks",
                app.managed_tasks.shutdown,
                timeout=4.0,
            )

    @app.after_serving
    async def shutdown():
        logger.info("application_shutdown")
        app._shutdown_started = True
        app.shutdown_event.set()

        # Quiesce published work before waiting for an overlapping startup.
        signal_error = None
        adapter = getattr(app, "tg_adapter", None)
        for signal in ((adapter.retire,) if adapter is not None else ()) + (
            app.events.close,
        ):
            try:
                signal()
            except BaseException as error:
                if signal_error is None:
                    signal_error = error

        owner = app._startup_owner
        if owner is asyncio.current_task():
            raise RuntimeError("Cannot shut down from the startup task")
        if (
            owner is not None
            and not owner.done()
            and not owner.cancelling()
            and not app._startup_unwinding
        ):
            owner.cancel()

        cancelled = False
        while True:
            try:
                await app._lifecycle_lock.acquire()
                break
            except asyncio.CancelledError:
                # Startup still owns its resources until its cleanup finishes.
                cancelled = True
        try:
            if app._startup_complete:
                await close_started_resources()
        finally:
            app._lifecycle_lock.release()
        if cancelled:
            raise asyncio.CancelledError
        if signal_error is not None:
            raise signal_error

    @app.context_processor
    async def inject_globals():
        from typing import cast
        from quart import current_app, has_request_context, request

        typed_app = cast(TypedQuart, current_app._get_current_object())  # noqa: SLF001
        interactor = typed_app.chat_interactor
        user_repo = typed_app.user_repo
        adapter = (
            getattr(request, "_account_adapter", None)
            if has_request_context()
            else None
        )
        login_check = (
            getattr(request, "_login_page_check", None)
            if has_request_context()
            else None
        )

        def check() -> None:
            raise_if_task_stopped()
            if login_check is not None:
                login_check()
            if adapter is not None:
                interactor._ensure_repository_current(adapter)
                if typed_app.tg_adapter is not adapter:
                    raise AccountChanged()

        async def load_debug_events():
            deadline = (
                asyncio.get_running_loop().time() + TEMPLATE_EVENT_HISTORY_TIMEOUT
            )
            try:
                check()
                return await run_with_task_deadline(
                    interactor.get_recent_events,
                    deadline,
                    name="template_event_history",
                )
            except LogUnavailable, TimeoutError:
                return None
            finally:
                check()

        check()
        try:
            user = await user_repo.get_user(1)
        finally:
            check()
        active = typed_app.tg_adapter
        return {
            "current_user": user,
            "account_token": active.account_token,
            "profile_name": active.display_name,
            "profile_avatar_id": active.profile_avatar_id,
            "load_debug_events": load_debug_events,
        }

    return app
