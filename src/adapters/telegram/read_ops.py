import asyncio
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Optional

from telethon import errors, functions, types, utils

from src.domain.models import SystemEvent
from src.infrastructure.logging import get_logger
from src.infrastructure.telegram_queue import WriteQueueFull

logger = get_logger(__name__)

ReadKey = tuple[int, Optional[int]]
DispatchFn = Callable[[SystemEvent], Awaitable[None]]
MAX_PENDING_READ_KEYS = 512


@dataclass(frozen=True)
class PendingRead:
    max_id: Optional[int]
    version: int
    queued_at: float


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
        self._retry_counts: dict[ReadKey, int] = {}
        self._closed = False

    def set_dispatch_fn(self, dispatch_fn: Optional[DispatchFn]) -> None:
        self._dispatch_fn = dispatch_fn

    async def mark_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int] = None,
        max_id: Optional[int] = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("Read operations are closed")
        key = (chat_id, topic_id)
        if (
            key not in self._active_keys
            and len(self._active_keys) >= MAX_PENDING_READ_KEYS
        ):
            raise WriteQueueFull("Too many pending Telegram reads; try again later")
        self._pending[key] = self._merge_pending(self._pending.get(key), max_id)

        if key in self._active_keys:
            return

        self._active_keys.add(key)
        if self._coalesce_delay > 0:
            self._schedule_enqueue(key)
        else:
            try:
                await self._enqueue_key(key)
            except BaseException:
                self._pending.pop(key, None)
                self._active_keys.discard(key)
                raise

    async def mark_selective_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int],
        message_id: int,
        validate: Callable[[], Awaitable[bool]],
        on_success: Callable[[], Awaitable[None]],
    ) -> None:
        await self.mark_automatic_as_read(
            chat_id, topic_id, message_id, validate, on_success, selective=True
        )

    async def mark_automatic_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int],
        max_id: int,
        validate: Callable[[], Awaitable[bool]],
        on_success: Callable[[], Awaitable[None]],
        *,
        selective: bool,
    ) -> None:
        """Queue a bounded automatic read separately from coalesced manual reads."""
        if self._closed:
            raise RuntimeError("Read operations are closed")
        if type(max_id) is not int or max_id <= 0:
            raise ValueError("Automatic reads require a positive message bound")

        async def _do() -> None:
            if self._closed or not await validate():
                return
            if selective and not await self.can_selectively_read(
                chat_id, topic_id, max_id
            ):
                return
            # Network state queries and flood waits may have yielded to rule changes.
            if self._closed or not await validate():
                return
            sent = await self._send_mark_as_read(
                chat_id, topic_id, max_id, origin="automatic", validate=validate
            )
            if not sent:
                return
            # An observer failure must not retry an already completed Telegram write.
            try:
                await on_success()
            except Exception:
                logger.exception("automatic_read_completion_failed", chat_id=chat_id)

        await self._write_queue.enqueue(_do, automatic=True)

    async def can_selectively_read(
        self, chat_id: int, topic_id: Optional[int], message_id: int
    ) -> bool:
        """Fail closed unless server state identifies this as the sole unread top message."""
        _, state = await self._scope_unread_state(chat_id, topic_id)
        if state is None:
            return False
        read_through, unread_count, top_message = state
        return (
            unread_count == 1
            and top_message == message_id
            and read_through < message_id
        )

    async def _scope_unread_state(
        self, chat_id: int, topic_id: Optional[int]
    ) -> tuple[Any, Optional[tuple[int, int, int]]]:
        peer = await self.client.get_input_entity(chat_id)
        if topic_id is None:
            result = await self.client(
                functions.messages.GetPeerDialogsRequest(
                    peers=[types.InputDialogPeer(peer=peer)]
                )
            )
            matches = [
                dialog
                for dialog in result.dialogs
                if utils.get_peer_id(dialog.peer) == chat_id
            ]
        else:
            result = await self.client(
                functions.messages.GetForumTopicsByIDRequest(
                    peer=peer, topics=[topic_id]
                )
            )
            matches = [topic for topic in result.topics if topic.id == topic_id]
        if len(matches) != 1:
            return peer, None
        state = matches[0]
        read_through = getattr(state, "read_inbox_max_id", None)
        unread_count = getattr(state, "unread_count", None)
        top_message = getattr(state, "top_message", None)
        if any(
            type(value) is not int or value < 0
            for value in (read_through, unread_count, top_message)
        ):
            return peer, None
        return peer, (read_through, unread_count, top_message)

    async def first_unread_message_id(
        self, chat_id: int, topic_id: Optional[int]
    ) -> Optional[int]:
        """Resolve the first unread incoming message from a stable server snapshot."""
        peer, state = await self._scope_unread_state(chat_id, topic_id)
        if state is None or state[1] == 0:
            return None
        messages = await self.client.get_messages(
            peer, limit=50, reverse=True, min_id=state[0], reply_to=topic_id
        )
        candidate = next(
            (
                msg.id
                for msg in messages
                if msg
                and not getattr(msg, "out", False)
                and state[0] < msg.id <= state[2]
            ),
            None,
        )
        if candidate is None:
            return None
        _, latest = await self._scope_unread_state(chat_id, topic_id)
        return candidate if latest == state else None

    def _merge_pending(
        self, existing: Optional[PendingRead], max_id: Optional[int]
    ) -> PendingRead:
        if existing is None:
            return PendingRead(max_id=max_id, version=1, queued_at=time.monotonic())
        return PendingRead(
            max_id=self._merge_max_id(existing.max_id, max_id),
            version=existing.version + 1,
            queued_at=existing.queued_at,
        )

    def _merge_max_id(
        self, existing: Optional[int], incoming: Optional[int]
    ) -> Optional[int]:
        if existing is None or incoming is None:
            return None
        return max(existing, incoming)

    def _schedule_enqueue(self, key: ReadKey, delay: Optional[float] = None) -> None:
        if self._closed:
            return
        task = asyncio.create_task(self._enqueue_after_delay(key, delay))
        self._enqueue_tasks.add(task)
        task.add_done_callback(self._enqueue_tasks.discard)

    async def _enqueue_after_delay(
        self, key: ReadKey, delay: Optional[float] = None
    ) -> None:
        try:
            await asyncio.sleep(self._coalesce_delay if delay is None else delay)
            await self._enqueue_key(key)
        except Exception as e:
            self._active_keys.discard(key)
            pending = self._pending.pop(key, None)
            self._retry_counts.pop(key, None)
            logger.error(
                "mark_as_read_enqueue_failed",
                chat_id=key[0],
                topic_id=key[1],
                error=repr(e),
                traceback=traceback.format_exc(),
            )
            if pending and self._dispatch_fn:
                try:
                    await self._dispatch_fn(
                        SystemEvent(
                            type="read_failed",
                            text="Read could not be queued",
                            chat_name="",
                            chat_id=key[0],
                            topic_id=key[1],
                            read_origin="manual",
                            read_through=pending.max_id,
                        )
                    )
                except Exception:
                    logger.exception("read_failure_dispatch_failed", chat_id=key[0])

    async def _enqueue_key(self, key: ReadKey) -> None:
        async def _do() -> None:
            await self._drain(key)

        await self._write_queue.enqueue(_do)

    async def _drain(self, key: ReadKey) -> None:
        if self._closed:
            return
        pending = self._pending.get(key)
        if pending is None:
            self._active_keys.discard(key)
            return

        logger.info(
            "mark_as_read_sending",
            chat_id=key[0],
            topic_id=key[1],
            max_id=pending.max_id,
            queue_seconds=round(time.monotonic() - pending.queued_at, 3),
        )
        try:
            await self._send_mark_as_read(key[0], key[1], pending.max_id)
        except errors.FloodWaitError:
            # The write queue owns Telegram's mandatory flood-wait backoff.
            raise
        except (OSError, asyncio.TimeoutError, errors.ServerError):
            attempt = min(self._retry_counts.get(key, 0) + 1, 6)
            self._retry_counts[key] = attempt
            delay = min(2 ** (attempt - 1), 30)
            logger.warning(
                "mark_as_read_retry",
                chat_id=key[0],
                topic_id=key[1],
                max_id=pending.max_id,
                delay_seconds=delay,
            )
            self._schedule_enqueue(key, delay=delay)
            return
        except Exception:
            # A permanent failure must not leave the chat stuck in active_keys,
            # silently swallowing every future read request for that chat.
            self._pending.pop(key, None)
            self._active_keys.discard(key)
            self._retry_counts.pop(key, None)
            if self._dispatch_fn:
                try:
                    await self._dispatch_fn(
                        SystemEvent(
                            type="read_failed",
                            text="Read failed",
                            chat_name="",
                            chat_id=key[0],
                            topic_id=key[1],
                            read_origin="manual",
                            read_through=pending.max_id,
                        )
                    )
                except Exception:
                    logger.exception("read_failure_dispatch_failed", chat_id=key[0])
            raise

        self._retry_counts.pop(key, None)

        if self._pending.get(key) == pending:
            self._pending.pop(key, None)
            self._active_keys.discard(key)
            return

        self._schedule_enqueue(key)

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._enqueue_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._enqueue_tasks.clear()
        self._pending.clear()
        self._active_keys.clear()
        self._retry_counts.clear()

    async def _send_mark_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int],
        max_id: Optional[int],
        *,
        origin: str = "manual",
        validate: Optional[Callable[[], Awaitable[bool]]] = None,
    ) -> bool:
        started = time.monotonic()
        try:
            input_peer = await self.client.get_input_entity(chat_id)
            # Peer resolution may suspend long enough for the account or rule
            # to change after the earlier check.
            if validate and (self._closed or not await validate()):
                return False
            if topic_id is None:
                topic_name = None
                await self._read_chat(input_peer, max_id)
            else:
                topic_name = await self._read_topic(
                    input_peer, chat_id, topic_id, max_id
                )

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
            raise

        logger.info(
            "mark_as_read_completed",
            chat_id=chat_id,
            topic_id=topic_id,
            max_id=max_id,
            rpc_seconds=round(time.monotonic() - started, 3),
        )
        try:
            await self._dispatch_read_event(
                chat_id, topic_id, topic_name, input_peer, origin, max_id
            )
        except Exception:
            logger.exception("read_event_dispatch_failed", chat_id=chat_id)
        return True

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
            try:
                return await self._get_topic_name_fn(chat_id, topic_id)
            except Exception:
                logger.warning(
                    "topic_name_after_read_failed", chat_id=chat_id, topic_id=topic_id
                )
                return None
        except Exception as e:
            if "TOPIC_ID_INVALID" in str(e):
                logger.warning(
                    "mark_read_topic_invalid",
                    chat_id=chat_id,
                    topic_id=topic_id,
                )
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
        origin: str,
        max_id: Optional[int],
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
                read_origin=origin,
                read_through=max_id,
                link=f"/chat/{chat_id}",
            )
        )
