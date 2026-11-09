import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Optional

from telethon import errors, functions, types, utils

from src.domain.errors import AccountChanged
from src.domain.models import SystemEvent
from src.infrastructure.logging import get_logger
from src.infrastructure.tasks import raise_if_task_stopped, run_with_task_deadline
from src.infrastructure.telegram_queue import WriteQueueFull

logger = get_logger(__name__)

ReadKey = tuple[int, Optional[int]]
DispatchFn = Callable[[SystemEvent], Awaitable[None]]
MAX_PENDING_READ_KEYS = 512
# shortcut: allow 10 seconds per best-effort post-read lookup or notification;
# revisit if legitimate operations routinely take longer.
POST_READ_UPDATE_TIMEOUT = 10


@dataclass(frozen=True)
class PendingRead:
    max_id: Optional[int]
    queued_at: float


class ReadNotConfirmed(RuntimeError):
    """Telegram did not return a confirmed read result."""


async def _run_queued_read(
    operation: Callable[[], Awaitable[None]], *, name: str
) -> None:
    async def checked() -> None:
        try:
            await operation()
        finally:
            raise_if_task_stopped()

    # Keep worker cancellation separate from a dependency that clears its own.
    await run_with_task_deadline(checked, None, name=name)


class ReadOps:
    def __init__(
        self,
        client: Any,
        write_queue: Any,
        dispatch_fn: Optional[DispatchFn],
        get_topic_name_fn: Callable[[int, int], Awaitable[Optional[str]]],
        coalesce_delay: float = 0,
        *,
        get_topic_name_before_fn: Optional[
            Callable[[int, int, float], Awaitable[Optional[str]]]
        ] = None,
    ) -> None:
        self.client = client
        self._write_queue = write_queue
        self._dispatch_fn = dispatch_fn
        self._get_topic_name_fn = get_topic_name_fn
        self._get_topic_name_before_fn = get_topic_name_before_fn
        self._coalesce_delay = coalesce_delay
        self._pending: dict[ReadKey, PendingRead] = {}
        self._active_keys: set[ReadKey] = set()
        self._inflight_keys: set[ReadKey] = set()
        self._following_reads: dict[ReadKey, PendingRead] = {}
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
        if key in self._inflight_keys:
            # Later reads must not inherit the in-flight bound once that
            # attempt completes, whether it succeeds or fails.
            self._following_reads[key] = self._merge_pending(
                self._following_reads.get(key), max_id
            )

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

        async def perform() -> None:
            raise_if_task_stopped()
            if self._closed:
                return
            valid = await validate()
            raise_if_task_stopped()
            if self._closed or not valid:
                return
            if selective:
                allowed = await self.can_selectively_read(chat_id, topic_id, max_id)
                raise_if_task_stopped()
                if self._closed or not allowed:
                    return
            # Network state queries and flood waits may have yielded to rule changes.
            valid = await validate()
            raise_if_task_stopped()
            if self._closed or not valid:
                return
            sent = await self._send_mark_as_read(
                chat_id, topic_id, max_id, origin="automatic", validate=validate
            )
            if not sent:
                return
            # An observer failure must not retry an already completed Telegram write.
            try:
                await on_success()
            except Exception as error:
                raise_if_task_stopped()
                logger.error(
                    "automatic_read_completion_failed",
                    chat_id=chat_id,
                    category=type(error).__name__,
                )
            raise_if_task_stopped()

        async def _do() -> None:
            await _run_queued_read(perform, name="queued_automatic_read")

        await self._write_queue.enqueue(_do, automatic=True)

    async def can_selectively_read(
        self,
        chat_id: int,
        topic_id: Optional[int],
        message_id: int,
        *,
        is_current: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """Fail closed unless server state identifies this as the sole unread top message."""

        def check() -> None:
            raise_if_task_stopped()
            if is_current is not None and not is_current():
                raise AccountChanged()

        check()
        _, state = await self._scope_unread_state(chat_id, topic_id, check=check)
        check()
        if state is None:
            return False
        read_through, unread_count, top_message = state
        return (
            unread_count == 1
            and top_message == message_id
            and read_through < message_id
        )

    async def _scope_unread_state(
        self, chat_id: int, topic_id: Optional[int], *, check: Callable[[], None]
    ) -> tuple[Any, Optional[tuple[int, int, int]]]:
        check()
        peer = await self.client.get_input_entity(chat_id)
        check()
        if topic_id is None:
            result = await self.client(
                functions.messages.GetPeerDialogsRequest(
                    peers=[types.InputDialogPeer(peer=peer)]
                )
            )
            check()
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
            check()
            matches = [topic for topic in result.topics if topic.id == topic_id]
        check()
        if len(matches) != 1:
            return peer, None
        state = matches[0]
        read_through = getattr(state, "read_inbox_max_id", None)
        unread_count = getattr(state, "unread_count", None)
        top_message = getattr(state, "top_message", None)
        check()
        if any(
            type(value) is not int or value < 0
            for value in (read_through, unread_count, top_message)
        ):
            return peer, None
        return peer, (read_through, unread_count, top_message)

    def _merge_pending(
        self, existing: Optional[PendingRead], max_id: Optional[int]
    ) -> PendingRead:
        if existing is None:
            return PendingRead(max_id=max_id, queued_at=time.monotonic())
        return PendingRead(
            max_id=self._merge_max_id(existing.max_id, max_id),
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
                category=type(e).__name__,
            )
            if pending and self._dispatch_fn:
                try:
                    await self._dispatch_read_failure(
                        SystemEvent(
                            type="read_failed",
                            text="Read could not be queued. Try again.",
                            chat_name="",
                            chat_id=key[0],
                            topic_id=key[1],
                            read_origin="manual",
                            read_through=pending.max_id,
                        )
                    )
                except Exception as error:
                    logger.error(
                        "read_failure_dispatch_failed",
                        chat_id=key[0],
                        category=type(error).__name__,
                    )

    async def _enqueue_key(self, key: ReadKey) -> None:
        async def _do() -> None:
            await _run_queued_read(lambda: self._drain(key), name="queued_manual_read")

        await self._write_queue.enqueue(_do)

    async def _drain(self, key: ReadKey) -> None:
        raise_if_task_stopped()
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
            self._inflight_keys.add(key)
            try:
                await self._send_mark_as_read(key[0], key[1], pending.max_id)
            finally:
                self._inflight_keys.discard(key)
        except errors.FloodWaitError:
            raise_if_task_stopped()
            # The write queue owns Telegram's mandatory flood-wait backoff.
            self._following_reads.pop(key, None)
            raise
        except (OSError, asyncio.TimeoutError, errors.ServerError):
            raise_if_task_stopped()
            self._following_reads.pop(key, None)
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
            raise_if_task_stopped()
            # A newer accepted read is independent of the failed write; keep
            # its own bound rather than dropping it or inheriting the old one.
            following = self._following_reads.pop(key, None)
            if following is None:
                self._pending.pop(key, None)
                self._active_keys.discard(key)
            else:
                self._pending[key] = following
            self._retry_counts.pop(key, None)
            try:
                if self._dispatch_fn:
                    await self._dispatch_read_failure(
                        SystemEvent(
                            type="read_failed",
                            text="Read was not confirmed. Refresh before retrying.",
                            chat_name="",
                            chat_id=key[0],
                            topic_id=key[1],
                            read_origin="manual",
                            read_through=pending.max_id,
                        )
                    )
            except Exception as error:
                logger.error(
                    "read_failure_dispatch_failed",
                    chat_id=key[0],
                    category=type(error).__name__,
                )
            finally:
                if following is not None:
                    self._schedule_enqueue(key)
            raise
        except BaseException:
            self._following_reads.pop(key, None)
            raise

        following = self._following_reads.pop(key, None)
        self._retry_counts.pop(key, None)

        if following is None:
            self._pending.pop(key, None)
            self._active_keys.discard(key)
            return

        self._pending[key] = following
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
        self._inflight_keys.clear()
        self._following_reads.clear()
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
            raise_if_task_stopped()
            if self._closed:
                return False
            input_peer = await self.client.get_input_entity(chat_id)
            raise_if_task_stopped()
            # Peer resolution may suspend long enough for the account or rule
            # to change after the earlier check.
            if self._closed:
                return False
            if validate:
                valid = await validate()
                raise_if_task_stopped()
                if self._closed or not valid:
                    return False
            if topic_id is None:
                topic_name = None
                await self._read_chat(input_peer, max_id)
            else:
                topic_name = await self._read_topic(
                    input_peer, chat_id, topic_id, max_id
                )
            raise_if_task_stopped()

        except errors.FloodWaitError:
            raise_if_task_stopped()
            raise
        except Exception as e:
            raise_if_task_stopped()
            logger.error(
                "mark_as_read_failed",
                chat_id=chat_id,
                topic_id=topic_id,
                category=type(e).__name__,
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
        except Exception as error:
            logger.error(
                "read_event_dispatch_failed",
                chat_id=chat_id,
                category=type(error).__name__,
            )
        raise_if_task_stopped()
        return True

    async def _read_chat(self, input_peer: Any, max_id: Optional[int]) -> None:
        if max_id is None:
            result = await self.client.send_read_acknowledge(input_peer)
        else:
            result = await self.client.send_read_acknowledge(input_peer, max_id=max_id)
        raise_if_task_stopped()
        if result is not True and not isinstance(
            result, types.messages.AffectedMessages
        ):
            raise ReadNotConfirmed("Telegram did not confirm the read")

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
                raise_if_task_stopped()
                if self._closed:
                    raise ReadNotConfirmed("Read operations are closed")

            result = await self.client(
                functions.messages.ReadDiscussionRequest(
                    peer=input_peer,
                    msg_id=topic_id,
                    read_max_id=read_max_id,
                )
            )
            raise_if_task_stopped()
            if result is not True:
                raise ReadNotConfirmed("Telegram did not confirm the topic read")
            loop = asyncio.get_running_loop()
            deadline = loop.time() + POST_READ_UPDATE_TIMEOUT

            async def topic_name() -> Optional[str]:
                if self._get_topic_name_before_fn:
                    return await self._get_topic_name_before_fn(
                        chat_id, topic_id, deadline
                    )
                return await self._get_topic_name_fn(chat_id, topic_id)

            try:
                return await run_with_task_deadline(
                    topic_name, deadline, name="post_read_topic_name"
                )
            except Exception as error:
                raise_if_task_stopped()
                logger.warning(
                    "topic_name_after_read_failed",
                    chat_id=chat_id,
                    topic_id=topic_id,
                    category=type(error).__name__,
                )
                return None
            finally:
                raise_if_task_stopped()
        except Exception as e:
            raise_if_task_stopped()
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

    async def _dispatch_read_failure(self, event: SystemEvent) -> None:
        dispatch_fn = self._dispatch_fn
        if dispatch_fn is None:
            return
        deadline = asyncio.get_running_loop().time() + POST_READ_UPDATE_TIMEOUT
        await run_with_task_deadline(
            lambda: dispatch_fn(event), deadline, name="read_failure_notification"
        )

    async def _dispatch_read_event(
        self,
        chat_id: int,
        topic_id: Optional[int],
        topic_name: Optional[str],
        input_peer: Any,
        origin: str,
        max_id: Optional[int],
    ) -> None:
        raise_if_task_stopped()
        if not self._dispatch_fn:
            return

        chat_name = f"Chat {chat_id}"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + POST_READ_UPDATE_TIMEOUT

        async def resolve_chat_name() -> str:
            entity = await self.client.get_entity(input_peer)
            raise_if_task_stopped()
            return utils.get_display_name(entity)

        try:
            chat_name = await run_with_task_deadline(
                resolve_chat_name, deadline, name="post_read_chat_name"
            )
        except Exception as error:
            raise_if_task_stopped()
            logger.warning(
                "read_event_chat_name_failed",
                chat_id=chat_id,
                category=type(error).__name__,
            )
        finally:
            raise_if_task_stopped()

        dispatch_fn = self._dispatch_fn
        if dispatch_fn is None:
            return
        event = SystemEvent(
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
        deadline = loop.time() + POST_READ_UPDATE_TIMEOUT
        await run_with_task_deadline(
            lambda: dispatch_fn(event), deadline, name="post_read_notification"
        )
