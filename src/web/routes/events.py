import asyncio
import json

from quart import Blueprint, current_app, render_template, request

from src.requests import optional_int
from src.web.events import EventBroadcaster, RESYNC, Subscription
from src.web.requests import BadRequest, query_values

events_bp = Blueprint("events", __name__)
# Bound the lifetime of a response abandoned before iteration starts.
HEARTBEAT_INTERVAL = 15.0


class EventStream:
    def __init__(
        self, broadcaster: EventBroadcaster, subscription: Subscription, adapter
    ):
        self.broadcaster = broadcaster
        self.subscription = subscription
        self.adapter = adapter
        self.snapshot = None
        self.before_snapshot = 0
        self.closed = False
        self.ready = False
        self.start_timeout = asyncio.get_running_loop().call_later(
            HEARTBEAT_INTERVAL, self.close
        )

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        self.start_timeout.cancel()
        if self.closed:
            raise StopAsyncIteration
        if not self.ready:
            self.ready = True
            return ": connected\n\n"
        if self.snapshot is not None and self.before_snapshot == 0:
            data, self.snapshot = self.snapshot, None
        else:
            try:
                async with asyncio.timeout(HEARTBEAT_INTERVAL):
                    data = await self.subscription.queue.get()
                    if self.before_snapshot:
                        self.before_snapshot -= 1
            except TimeoutError:
                data = None
            except BaseException:
                self.close()
                raise
        if self.adapter._retired:
            self.close()
            return 'data: {"type":"account_changed"}\n\n'
        if not self.adapter.is_connected() or self.subscription.stale:
            self.close()
            data = RESYNC
        if data is None:
            return ": heartbeat\n\n"
        return f"data: {data}\n\n"

    def close(self) -> None:
        self.closed = True
        self.start_timeout.cancel()
        self.broadcaster.unsubscribe(self.subscription)

    async def aclose(self) -> None:
        self.close()


@events_bp.route("/api/events/stream")
async def event_stream():
    if "text/event-stream" not in request.accept_mimetypes:
        return "SSE only", 400
    try:
        params = query_values({"chat_id", "topic_id", "debug", "snapshot", "account"})
        chat_id = optional_int(params, "chat_id")
        topic_id = optional_int(params, "topic_id")
        debug = params.get("debug", "0")
        snapshot = params.get("snapshot")
        if (
            (topic_id is not None and chat_id is None)
            or debug not in {"0", "1"}
            or (snapshot is not None and not 1 <= len(snapshot) <= 128)
        ):
            raise BadRequest("Invalid subscription")
    except BadRequest:
        return {"error": "Invalid subscription"}, 400

    if request.method == "HEAD":
        return current_app.response_class(mimetype="text/event-stream")

    adapter = request._account_adapter
    if params.get("account") != adapter.account_token:
        return current_app.response_class(
            'data: {"type":"account_changed"}\n\n', mimetype="text/event-stream"
        )

    broadcaster = current_app.events
    subscription = broadcaster.subscribe(chat_id, topic_id, debug == "1")
    if subscription is None:
        return "Too many event connections", 503
    stream = None
    try:
        frame = None
        before_snapshot = 0
        if snapshot is not None:
            # Subscribe before reading history so later changes are queued.
            interactor = current_app.chat_interactor
            if chat_id is None:
                if not interactor.is_chat_snapshot_current(snapshot):
                    frame = '{"type":"snapshot_stale"}'
            elif snapshot != interactor.transcript_revision(chat_id, topic_id):
                messages = await interactor.get_chat_messages(
                    chat_id, topic_id=topic_id
                )
                # Apply queued events that the history may already reflect
                # before the snapshot, so they cannot overwrite newer state.
                before_snapshot = subscription.queue.qsize()
                html = await render_template(
                    "chat/transcript.html.j2", messages=messages, chat_id=chat_id
                )
                frame = json.dumps({"type": "transcript_snapshot", "html": html})
        stream = EventStream(broadcaster, subscription, adapter)
        stream.snapshot = frame
        stream.before_snapshot = before_snapshot
        response = current_app.response_class(stream, mimetype="text/event-stream")
        response.headers["X-Accel-Buffering"] = "no"
        response.timeout = None
    except BaseException:
        if stream is None:
            broadcaster.unsubscribe(subscription)
        else:
            stream.close()
        raise
    return response
