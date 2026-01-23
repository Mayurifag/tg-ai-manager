import asyncio
import json
from dataclasses import asdict

from quart import Blueprint, make_response, render_template, request

from src.domain.models import SystemEvent
from src.web.serializers import json_serializer

sse_bp = Blueprint("sse", __name__)

# Global state for SSE
connected_queues = set()
shutdown_event = asyncio.Event()


async def sse_subscriber(event: SystemEvent):
    """
    Subscriber that receives events from EventBus and pushes them to SSE clients.
    Pre-renders HTML templates where necessary.
    """
    # 1. Render HTML for frontend
    if event.type == "message" and event.message_model:
        # We need an app context to render templates.
        # Note: This runs in the event bus context.
        # If quart is not thread-local safe in this context, we might need manual context pushing.
        # However, render_template in Quart usually works if we are inside the loop.
        try:
            event.rendered_html = await render_template(
                "chat/messages_partial.html.j2",
                messages=[event.message_model],
                chat_id=event.chat_id,
            )
        except Exception:
            # Fallback or log if rendering fails outside request context
            pass

    # 2. Serialize
    data = json.dumps(asdict(event), default=json_serializer)

    # 3. Broadcast
    for queue in connected_queues:
        await queue.put(data)


@sse_bp.route("/api/events/stream")
async def event_stream():
    if "text/event-stream" not in request.accept_mimetypes:
        return "SSE only", 400

    queue = asyncio.Queue()
    connected_queues.add(queue)

    async def generator():
        try:
            while not shutdown_event.is_set():
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        except GeneratorExit:
            pass
        finally:
            connected_queues.discard(queue)

    response = await make_response(generator())
    setattr(response, "timeout", None)
    response.headers["Content-Type"] = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Connection"] = "keep-alive"
    return response
