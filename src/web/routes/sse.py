import asyncio
from quart import Blueprint, current_app, request, make_response
from src.web.sse import RESYNC

sse_bp = Blueprint("sse", __name__)


@sse_bp.route("/api/events/stream")
async def event_stream():
    if "text/event-stream" not in request.accept_mimetypes:
        return "SSE only", 400

    try:
        chat_id = int(request.args["chat_id"]) if "chat_id" in request.args else None
        topic_id = int(request.args["topic_id"]) if "topic_id" in request.args else None
        if (chat_id is not None and abs(chat_id) >= 2**63) or (
            topic_id is not None and not 0 < topic_id < 2**31
        ):
            raise ValueError
    except ValueError:
        return "Invalid subscription", 400
    broadcaster = current_app.sse
    subscription = broadcaster.subscribe(
        chat_id, topic_id, request.args.get("debug") == "1"
    )
    if subscription is None:
        return "Too many event streams", 503

    async def generator():
        try:
            while True:
                try:
                    data = await asyncio.wait_for(
                        subscription.queue.get(), timeout=15.0
                    )
                    yield f"data: {data}\n\n"
                    if data == RESYNC:
                        break
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        except GeneratorExit:
            pass
        finally:
            broadcaster.unsubscribe(subscription)

    response = await make_response(generator())

    setattr(response, "timeout", None)

    response.headers["Content-Type"] = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Connection"] = "keep-alive"
    return response
