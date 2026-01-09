import asyncio
import os
import sys

# Alembic Imports
from alembic import command
from alembic.config import Config as AlembicConfig
from quart import Quart

from src.container import (
    get_action_repo,
    get_chat_interactor,
    get_event_repo,
    get_rule_service,
)
from src.infrastructure.logging import configure_logging, get_logger
from src.jinja_filters import file_mtime_filter
from src.web.routes import register_routes
from src.web.sse import broadcast_event, connected_queues, shutdown_event

logger = get_logger(__name__)


def run_migrations(root_path: str):
    """Executes Alembic migrations programmatically."""
    try:
        logger.info("running_migrations")
        alembic_ini_path = os.path.join(root_path, "alembic.ini")
        alembic_cfg = AlembicConfig(alembic_ini_path)

        # Ensure we point to the correct script location relative to root
        script_location = os.path.join(root_path, "migrations")
        alembic_cfg.set_main_option("script_location", script_location)

        # Run upgrade head
        command.upgrade(alembic_cfg, "head")
        logger.info("migrations_completed")
    except Exception as e:
        logger.error("migrations_failed", error=str(e))
        # Decide if we should exit here. Usually yes, if DB is invalid.
        sys.exit(1)


def create_app() -> Quart:
    # Configure structured logging
    configure_logging()

    # Set the project root explicitly
    root_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))

    app = Quart(__name__, root_path=root_path, template_folder="src/templates")

    # Register filters
    file_mtime_filter(app)

    # Register routes
    register_routes(app)

    @app.before_serving
    async def startup():
        logger.info("application_startup")

        # 1. Run Database Migrations
        # Running synchronously is fine here as it blocks startup until DB is ready
        run_migrations(root_path)

        # Reset shutdown event
        shutdown_event.clear()

        interactor = get_chat_interactor()
        await interactor.initialize()

        # We wrap broadcast_event to ensure it runs within an app context
        async def _context_aware_broadcast(event):
            async with app.app_context():
                await broadcast_event(event)

        await interactor.subscribe_to_events(_context_aware_broadcast)

        # Trigger startup scan in background
        rule_service = get_rule_service()
        asyncio.create_task(rule_service.run_startup_scan())

        # Start maintenance background job (Logs & File Cache)
        asyncio.create_task(job_background_maintenance())

    @app.after_serving
    async def shutdown():
        logger.info("application_shutdown")
        shutdown_event.set()

        # Allow SSE generators to exit gracefully
        await asyncio.sleep(0.1)

        interactor = get_chat_interactor()
        try:
            await asyncio.wait_for(interactor.shutdown(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.error("telegram_shutdown_timeout")
        except Exception as e:
            logger.error("shutdown_error", error=str(e))

        connected_queues.clear()

    @app.context_processor
    async def inject_recent_events():
        """
        Injects recent events into the template context.
        Async context processors are supported in Quart.
        """
        interactor = get_chat_interactor()
        events = await interactor.get_recent_events()
        return {"recent_events": events}

    return app


async def job_background_maintenance():
    """Background task to clean old logs and enforce cache limits."""
    action_repo = get_action_repo()
    event_repo = get_event_repo()
    interactor = get_chat_interactor()

    logger.info("maintenance_job_started")
    while not shutdown_event.is_set():
        try:
            # Clean Valkey Logs
            await action_repo.cleanup_expired()
            await event_repo.cleanup_expired()

            # Clean File Cache
            await interactor.run_storage_maintenance()
        except Exception as e:
            logger.error("maintenance_job_error", error=str(e))

        # Run every hour
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass
