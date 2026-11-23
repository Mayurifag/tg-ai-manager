import asyncio
import json
from dataclasses import asdict, dataclass, replace
from types import SimpleNamespace
from typing import Optional

from quart import current_app

from src.domain.models import SystemEvent
from src.infrastructure.html import html_to_text
from src.infrastructure.tasks import raise_if_task_stopped
from src.web.serializers import json_serializer

MAX_EVENT_CLIENTS = 50
EVENT_QUEUE_SIZE = 128
RESYNC = json.dumps({"type": "resync_required"})


@dataclass(eq=False)
class Subscription:
    queue: asyncio.Queue[str]
    chat_id: Optional[int] = None
    topic_id: Optional[int] = None
    debug: bool = False
    stale: bool = False


class EventBroadcaster:
    """App-owned, bounded subscriptions with explicit overflow recovery."""

    def __init__(
        self, max_clients: int = MAX_EVENT_CLIENTS, queue_size: int = EVENT_QUEUE_SIZE
    ):
        self._clients: set[Subscription] = set()
        self._max_clients = max_clients
        self._queue_size = queue_size
        self._closed = False

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def subscribe(
        self,
        chat_id: Optional[int] = None,
        topic_id: Optional[int] = None,
        debug: bool = False,
    ) -> Optional[Subscription]:
        if self._closed or len(self._clients) >= self._max_clients:
            return None
        client = Subscription(
            asyncio.Queue(maxsize=self._queue_size), chat_id, topic_id, debug
        )
        self._clients.add(client)
        return client

    def unsubscribe(self, client: Subscription) -> None:
        self._clients.discard(client)

    def invalidate_clients(self) -> None:
        for client in tuple(self._clients):
            self._resync(client)

    def close(self) -> None:
        self._closed = True
        self.invalidate_clients()

    @staticmethod
    def _resync(client: Subscription) -> None:
        if client.stale:
            return
        client.stale = True
        while not client.queue.empty():
            client.queue.get_nowait()
        client.queue.put_nowait(RESYNC)

    async def broadcast(self, event: SystemEvent) -> None:
        raise_if_task_stopped()
        if event.type == "resync_required":
            # Do not deliver queued events from a state that is no longer known
            # to be current. Wake the stream handlers so clients can reconnect.
            self.invalidate_clients()
            return
        clients = tuple(client for client in self._clients if not client.stale)
        if not clients:
            return

        payload = asdict(replace(event, message_model=None, rendered_html=None))
        if event.chat_id is not None:
            payload["chat_id"] = str(event.chat_id)
        payload["text_plain"] = html_to_text(event.text)
        serialized_summary = None
        message = None
        serialized_full: dict[tuple[bool, bool], str] = {}

        for client in clients:
            if client.stale:
                continue
            same_chat = client.chat_id is not None and client.chat_id == event.chat_id
            same_topic = client.topic_id == event.topic_id
            # Deletions and reaction updates may not include a topic, but have
            # a message ID that a transcript can match without guessing.
            full = same_chat and (
                same_topic
                or event.type
                in {
                    "deleted",
                    "reaction_update",
                    "reaction_failed",
                    "reaction_uncertain",
                    "reaction_blocked",
                }
            )
            if not full:
                if serialized_summary is None:
                    summary = payload
                    if event.message_model is not None:
                        summary = dict(
                            payload,
                            message_model={
                                "id": event.message_model.id,
                                "date": event.message_model.date,
                                "is_outgoing": event.message_model.is_outgoing,
                            },
                        )
                    serialized_summary = json.dumps(summary, default=json_serializer)
                data = serialized_summary
            else:
                render = (
                    same_topic
                    and event.scope_known
                    and event.message_model is not None
                    and event.type in {"message", "action", "edited"}
                )
                key = (render, client.debug if render else False)
                if key not in serialized_full:
                    if message is None and event.message_model is not None:
                        message = asdict(event.message_model)
                        # These Telegram IDs exceed JavaScript's safe integer range.
                        if message["grouped_id"] is not None:
                            message["grouped_id"] = str(message["grouped_id"])
                        for reaction in message["reactions"]:
                            if reaction["custom_emoji_id"] is not None:
                                reaction["custom_emoji_id"] = str(
                                    reaction["custom_emoji_id"]
                                )
                    full_payload = dict(payload, message_model=message)
                    if render:
                        template = current_app.jinja_env.get_template(
                            "chat/messages_partial.html.j2"
                        )
                        full_payload["rendered_html"] = await template.render_async(
                            messages=[event.message_model],
                            chat_id=event.chat_id,
                            current_user=SimpleNamespace(debug_mode=client.debug),
                        )
                        raise_if_task_stopped()
                    serialized_full[key] = json.dumps(
                        full_payload, default=json_serializer
                    )
                data = serialized_full[key]
            # Rendering may have yielded while account replacement invalidated
            # this subscription. Do not enqueue old-account data after resync.
            if client.stale:
                continue
            raise_if_task_stopped()
            try:
                client.queue.put_nowait(data)
            except asyncio.QueueFull:
                self._resync(client)
