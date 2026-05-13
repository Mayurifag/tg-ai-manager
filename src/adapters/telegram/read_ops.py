import asyncio
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Optional

from telethon import errors, functions, utils

from src.domain.models import SystemEvent
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

ReadKey = tuple[int, Optional[int]]
DispatchFn = Callable[[SystemEvent], Awaitable[None]]


@dataclass(frozen=True)
class PendingRead:
    max_id: Optional[int]
    version: int


class ReadOps:
    def __init__(
        self,
        client: Any,
        write_queue: Any,
        dispatch_fn: Optional[DispatchFn],
        get_topic_name_fn: Callable[[int, int], Awaitable[Optional[str]]],
        coalesce_delay: float = 0,
    ) -> None:
        self.client = client
        self._write_queue = write_queue
        self._dispatch_fn = dispatch_fn
        self._get_topic_name_fn = get_topic_name_fn
        self._coalesce_delay = coalesce_delay
        self._pending: dict[ReadKey, PendingRead] = {}
        self._active_keys: set[ReadKey] = set()
        self._enqueue_tasks: set[asyncio.Task[None]] = set()

    def set_dispatch_fn(self, dispatch_fn: Optional[DispatchFn]) -> None:
        self._dispatch_fn = dispatch_fn

    async def mark_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int] = None,
        max_id: Optional[int] = None,
    ) -> None:
        key = (chat_id, topic_id)
        self._pending[key] = self._merge_pending(self._pending.get(key), max_id)

        if key in self._active_keys:
            return

        self._active_keys.add(key)
        if self._coalesce_delay > 0:
            self._schedule_enqueue(key)
        else:
            await self._enqueue_key(key)

    def _merge_pending(
        self, existing: Optional[PendingRead], max_id: Optional[int]
    ) -> PendingRead:
        if existing is None:
            return PendingRead(max_id=max_id, version=1)
        return PendingRead(
            max_id=self._merge_max_id(existing.max_id, max_id),
            version=existing.version + 1,
        )

    def _merge_max_id(
        self, existing: Optional[int], incoming: Optional[int]
    ) -> Optional[int]:
        if existing is None or incoming is None:
            return None
        return max(existing, incoming)

    def _schedule_enqueue(self, key: ReadKey) -> None:
        task = asyncio.create_task(self._enqueue_after_delay(key))
        self._enqueue_tasks.add(task)
        task.add_done_callback(self._enqueue_tasks.discard)

    async def _enqueue_after_delay(self, key: ReadKey) -> None:
        try:
            await asyncio.sleep(self._coalesce_delay)
            await self._enqueue_key(key)
        except Exception as e:
            self._active_keys.discard(key)
            logger.error(
                "mark_as_read_enqueue_failed",
                chat_id=key[0],
                topic_id=key[1],
                error=repr(e),
                traceback=traceback.format_exc(),
            )

    async def _enqueue_key(self, key: ReadKey) -> None:
        async def _do() -> None:
            await self._drain(key)

        await self._write_queue.enqueue(_do)

    async def _drain(self, key: ReadKey) -> None:
        pending = self._pending.get(key)
        if pending is None:
            self._active_keys.discard(key)
            return

        await self._send_mark_as_read(key[0], key[1], pending.max_id)

        if self._pending.get(key) == pending:
            self._pending.pop(key, None)
            self._active_keys.discard(key)
            return

        self._schedule_enqueue(key)

    async def _send_mark_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int],
        max_id: Optional[int],
    ) -> None:
        try:
            input_peer = await self.client.get_input_entity(chat_id)
            if topic_id is None:
                topic_name = None
                await self._read_chat(input_peer, max_id)
            else:
                topic_name = await self._read_topic(
                    input_peer, chat_id, topic_id, max_id
                )

            await self._dispatch_read_event(chat_id, topic_id, topic_name, input_peer)
        except errors.FloodWaitError:
            raise
        except Exception as e:
            logger.error(
                "mark_as_read_failed",
                chat_id=chat_id,
                topic_id=topic_id,
                error=repr(e),
                traceback=traceback.format_exc(),
            )

    async def _read_chat(self, input_peer: Any, max_id: Optional[int]) -> None:
        if max_id is None:
            await self.client.send_read_acknowledge(input_peer)
            return
        await self.client.send_read_acknowledge(input_peer, max_id=max_id)

    async def _read_topic(
        self,
        input_peer: Any,
        chat_id: int,
        topic_id: int,
        max_id: Optional[int],
    ) -> Optional[str]:
        try:
            read_max_id = max_id
            if read_max_id is None:
                read_max_id = await self._latest_topic_message_id(input_peer, topic_id)

            await self.client(
                functions.messages.ReadDiscussionRequest(
                    peer=input_peer,
                    msg_id=topic_id,
                    read_max_id=read_max_id,
                )
            )
            return await self._get_topic_name_fn(chat_id, topic_id)
        except Exception as e:
            if "TOPIC_ID_INVALID" in str(e):
                logger.warning(
                    "mark_read_topic_invalid",
                    chat_id=chat_id,
                    topic_id=topic_id,
                )
                return None
            raise

    async def _latest_topic_message_id(self, input_peer: Any, topic_id: int) -> int:
        msgs = await self.client.get_messages(input_peer, limit=1, reply_to=topic_id)
        return msgs[0].id if msgs else topic_id

    async def _dispatch_read_event(
        self,
        chat_id: int,
        topic_id: Optional[int],
        topic_name: Optional[str],
        input_peer: Any,
    ) -> None:
        if not self._dispatch_fn:
            return

        chat_name = f"Chat {chat_id}"
        try:
            entity = await self.client.get_entity(input_peer)
            chat_name = utils.get_display_name(entity)
        except Exception:
            pass

        await self._dispatch_fn(
            SystemEvent(
                type="read",
                text="Marked as read",
                chat_name=chat_name,
                topic_name=topic_name,
                chat_id=chat_id,
                topic_id=topic_id,
                is_read=True,
                link=f"/chat/{chat_id}",
            )
        )
