import asyncio
import json
import os
import sys

from alembic import command
from alembic.config import Config as AlembicConfig
from quart import Quart

from src.config import get_settings
from src.container import (
    get_action_repo,
    get_chat_interactor,
    get_embedded_worker,
    get_event_bus,
    get_event_repo,
    get_queue_service,
    get_rule_service,
    get_user_repo,
)
from src.handlers.skip_ads_handler import SkipAdsHandler
from src.infrastructure.logging import configure_logging, get_logger
from src.jinja_filters import file_mtime_filter
from src.settings.sqlite_repo import SqliteSettingsRepository
from src.web.routes import register_routes
from src.web.serializers import json_serializer
from src.web.sse import connected_queues, shutdown_event, sse_subscriber

logger = get_logger(__name__)


def run_migrations(root_path: str):
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


def create_app() -> Quart:
    configure_logging()
    root_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
    app = Quart(__name__, root_path=root_path, template_folder="src/templates")

    file_mtime_filter(app)

    @app.template_filter("to_json")
    def to_json_filter(obj):
        return json.dumps(obj, default=json_serializer, indent=2)

    register_routes(app)

    @app.before_serving
    async def startup():
        logger.info("application_startup")
        run_migrations(root_path)
        shutdown_event.clear()

        interactor = get_chat_interactor()
        await interactor.initialize()

        worker = get_embedded_worker()
        await worker.start()

        bus = get_event_bus()
        rule_service = get_rule_service()

        # 1. Rules Engine
        bus.subscribe(rule_service.handle_new_message_event)

        # 2. AI Skip Ads Handler
        settings_cfg = get_settings()
        settings_repo = SqliteSettingsRepository(db_path=settings_cfg.DB_PATH)
        action_repo = get_action_repo()
        queue_svc = get_queue_service()

        skip_ads_handler = SkipAdsHandler(settings_repo, action_repo, queue_svc)
        bus.subscribe(skip_ads_handler.handle)

        # 3. Event Persistence
        bus.subscribe(lambda e: get_event_repo().add_event(e))

        # 4. SSE Broadcaster
        async def context_aware_sse(event):
            async with app.app_context():
                await sse_subscriber(event)

        bus.subscribe(context_aware_sse)

        asyncio.create_task(rule_service.run_startup_scan())
        asyncio.create_task(job_background_maintenance())

    @app.after_serving
    async def shutdown():
        logger.info("application_shutdown")
        shutdown_event.set()
        await asyncio.sleep(0.1)

        worker = get_embedded_worker()
        await worker.stop()

        interactor = get_chat_interactor()
        try:
            await asyncio.wait_for(interactor.shutdown(), timeout=3.0)
        except Exception:
            pass

        connected_queues.clear()

    @app.context_processor
    async def inject_globals():
        interactor = get_chat_interactor()
        user_repo = get_user_repo()
        events = await interactor.get_recent_events()
        user = await user_repo.get_user(1)
        return {"recent_events": events, "current_user": user}

    return app


async def job_background_maintenance():
    action_repo = get_action_repo()
    from src.container import get_event_repo, get_user_repo

    event_repo = get_event_repo()
    interactor = get_chat_interactor()
    user_repo = get_user_repo()

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

        except Exception as e:
            logger.error("maintenance_job_error", error=str(e))

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass
