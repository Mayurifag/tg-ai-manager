import asyncio
import os
from quart import Quart
from src.web.routes import register_routes
from src.web.sse import broadcast_event, shutdown_event, connected_queues
from src.container import get_chat_interactor, get_rule_service
from src.jinja_filters import file_mtime_filter
from src.infrastructure.logging import configure_logging, get_logger

logger = get_logger(__name__)

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
        return {'recent_events': events}

    return app
