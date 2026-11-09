import asyncio
from quart import Blueprint, current_app, request, jsonify
from src.requests import optional_int
from src.web.requests import BadRequest, query_values
from src.web.sse import EventBroadcaster, RESYNC, Subscription

sse_bp = Blueprint("sse", __name__)
# shortcut: release an unstarted response after 15 seconds; revisit if the
# server legitimately delays beginning SSE bodies longer than this.
STREAM_START_TIMEOUT = 15.0
HEARTBEAT_INTERVAL = 15.0


class _EventStream:
    """Own a subscription even when Quart closes the body before iteration."""

    def __init__(self, broadcaster: EventBroadcaster, subscription: Subscription):
        self._broadcaster = broadcaster
        self._subscription = subscription
        self._closed = False
        self._finished = False
        self._start_timeout = asyncio.get_running_loop().call_later(
            STREAM_START_TIMEOUT, self._close
        )

    def __aiter__(self):
        return self

    async def __anext__(self):
        self._start_timeout.cancel()
        if self._closed or self._finished:
            self._close()
            raise StopAsyncIteration
        try:
            try:
                data = await asyncio.wait_for(
                    self._subscription.queue.get(), timeout=HEARTBEAT_INTERVAL
                )
            except asyncio.TimeoutError:
                return ": heartbeat\n\n"
            if data == RESYNC:
                self._finished = True
            return f"data: {data}\n\n"
        except BaseException:
            self._close()
            raise

    def _close(self):
        if not self._closed:
            self._closed = True
            self._start_timeout.cancel()
            self._broadcaster.unsubscribe(self._subscription)

    async def aclose(self):
        self._close()


@sse_bp.route("/api/events/stream")
async def event_stream():
    if "text/event-stream" not in request.accept_mimetypes:
        return "SSE only", 400

    try:
        params = query_values({"chat_id", "topic_id", "debug"})
        chat_id = optional_int(params, "chat_id")
        topic_id = optional_int(params, "topic_id")
        debug = params.get("debug", "0")
        if (topic_id is not None and chat_id is None) or debug not in {"0", "1"}:
            raise BadRequest("Invalid subscription")
    except BadRequest:
        return jsonify({"error": "Invalid subscription"}), 400
    broadcaster = current_app.sse
    subscription = broadcaster.subscribe(chat_id, topic_id, debug == "1")
    if subscription is None:
        return "Too many event streams", 503

    if request.method == "HEAD":
        broadcaster.unsubscribe(subscription)
        response = current_app.response_class()
    else:
        stream = _EventStream(broadcaster, subscription)
        try:
            response = current_app.response_class(stream)
        except BaseException:
            await stream.aclose()
            raise

    setattr(response, "timeout", None)

    response.headers["Content-Type"] = "text/event-stream"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Connection"] = "keep-alive"
    return response
