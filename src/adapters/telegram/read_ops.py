import asyncio
import time
from collections import OrderedDict
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
MAX_DELAYED_AUTOMATIC_READS = 256
MAX_CONFIRMED_READ_KEYS = 512
MAX_READ_ATTEMPTS = 4
# shortcut: allow 10 seconds per best-effort post-read lookup or notification;
# revisit if legitimate operations routinely take longer.
POST_READ_UPDATE_TIMEOUT = 10


@dataclass(frozen=True)
class PendingRead:
    max_id: Optional[int]
    queued_at: float


class ReadNotConfirmed(RuntimeError):
    """Telegram did not return a confirmed read result."""

    def __init__(self, *, response: Any):
        super().__init__("Telegram did not confirm the read")
        self.outcome = "false" if response is False else type(response).__name__


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
    ) -> None:
        self.client = client
        self._write_queue = write_queue
        self._dispatch_fn = dispatch_fn
        self._pending: dict[ReadKey, PendingRead] = {}
        self._active_keys: set[ReadKey] = set()
        self._delayed_automatic_reads = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._confirmed_reads: OrderedDict[ReadKey, int] = OrderedDict()
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
        try:
            await self._enqueue_key(key)
        except BaseException:
            self._pending.pop(key, None)
            self._active_keys.discard(key)
            raise

    async def mark_automatic_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int],
        max_id: int,
        validate: Callable[[], Awaitable[bool]],
        on_success: Callable[[], Awaitable[None]],
        *,
        selective: bool,
        chat_name: str,
        topic_name: Optional[str],
    ) -> bool:
        """Queue a bounded automatic read separately from coalesced manual reads."""
        if self._closed:
            raise RuntimeError("Read operations are closed")
        if type(max_id) is not int or max_id <= 0:
            raise ValueError("Automatic reads require a positive message bound")
        key = (chat_id, topic_id)
        if self._confirmed_reads.get(key, 0) >= max_id:
            return False
        queued_at = time.monotonic()

        async def perform() -> None:
            raise_if_task_stopped()
            # A preceding write may have confirmed this bound while we waited.
            if self._closed or self._confirmed_reads.get(key, 0) >= max_id:
                return
            queue_seconds = time.monotonic() - queued_at
            valid = await validate()
            raise_if_task_stopped()
            if self._closed or not valid:
                return
            if selective:
                allowed = await self.can_selectively_read(chat_id, topic_id, max_id)
                raise_if_task_stopped()
                if self._closed or not allowed:
                    return
            # _send_mark_as_read revalidates after any network state lookup.
            sent = await self._send_mark_as_read(
                chat_id,
                topic_id,
                max_id,
                origin="automatic",
                validate=validate,
                chat_name=chat_name,
                topic_name=topic_name,
                queue_seconds=queue_seconds,
            )
            if not sent:
                return
            self._start_task(self._notify(on_success))

        async def retry_after(delay: int) -> None:
            try:
                await asyncio.sleep(delay)
                if not self._closed:
                    await self._write_queue.enqueue(attempt_read, automatic=True)
            except RuntimeError as error:
                logger.warning(
                    "automatic_read_retry_not_queued",
                    chat_id=chat_id,
                    topic_id=topic_id,
                    category=type(error).__name__,
                )
            finally:
                self._delayed_automatic_reads -= 1

        attempts = 0

        async def attempt_read() -> None:
            nonlocal attempts
            attempts += 1
            try:
                await _run_queued_read(perform, name="queued_automatic_read")
            except (errors.FloodWaitError, errors.FloodPremiumWaitError) as error:
                if (
                    attempts >= MAX_READ_ATTEMPTS
                    or self._delayed_automatic_reads >= MAX_DELAYED_AUTOMATIC_READS
                ):
                    raise
                delay = max(1, error.seconds)
                logger.warning(
                    "automatic_read_flood_wait",
                    chat_id=chat_id,
                    topic_id=topic_id,
                    seconds=delay,
                )
                self._delayed_automatic_reads += 1
                self._start_task(retry_after(delay))

        await self._write_queue.enqueue(attempt_read, automatic=True)
        return True

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

    def _start_task(self, operation) -> None:
        task = asyncio.create_task(operation)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _enqueue_after_delay(
        self, key: ReadKey, pending: PendingRead, attempt: int, delay: float
    ) -> None:
        try:
            await asyncio.sleep(delay)
            if not self._closed:
                await self._enqueue_key(key, pending, attempt)
        except Exception as error:
            self._report_manual_failure(key, pending, error)
            await self._finish_manual_read(key)

    async def _enqueue_key(
        self, key: ReadKey, pending: PendingRead | None = None, attempt: int = 1
    ) -> None:
        async def _do() -> None:
            await _run_queued_read(
                lambda: self._drain(key, pending, attempt), name="queued_manual_read"
            )

        await self._write_queue.enqueue(_do)

    async def _finish_manual_read(self, key: ReadKey) -> None:
        if self._closed or key not in self._pending:
            self._active_keys.discard(key)
            return
        try:
            await self._enqueue_key(key)
        except Exception as error:
            self._active_keys.discard(key)
            self._report_manual_failure(key, self._pending.pop(key), error)

    def _report_manual_failure(
        self, key: ReadKey, pending: PendingRead, error: Exception
    ) -> None:
        logger.warning(
            "manual_read_failed",
            chat_id=key[0],
            topic_id=key[1],
            category=type(error).__name__,
        )
        self._emit_read(key, pending.max_id, "manual", failed=True)

    async def _drain(
        self, key: ReadKey, pending: PendingRead | None, attempt: int
    ) -> None:
        raise_if_task_stopped()
        if self._closed:
            return
        if pending is None:
            pending = self._pending.pop(key, None)
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
            if pending.max_id is None:
                _, state = await self._scope_unread_state(
                    *key, check=raise_if_task_stopped
                )
                if state is None:
                    raise LookupError("Telegram did not return a read bound")
                pending = PendingRead(state[2], pending.queued_at)
            if pending.max_id > 0:
                await self._send_mark_as_read(*key, pending.max_id)
            else:
                self._emit_read(key, pending.max_id, "manual")
        except asyncio.CancelledError:
            raise_if_task_stopped()
            self._report_manual_failure(
                key, pending, ConnectionError("Read interrupted")
            )
        except Exception as error:
            raise_if_task_stopped()
            flood = isinstance(
                error, (errors.FloodWaitError, errors.FloodPremiumWaitError)
            )
            if attempt < MAX_READ_ATTEMPTS and (
                flood
                or isinstance(
                    error, (OSError, errors.ServerError, errors.TimedOutError)
                )
            ):
                delay = error.seconds if flood else 2 ** (attempt - 1)
                self._start_task(
                    self._enqueue_after_delay(key, pending, attempt + 1, max(1, delay))
                )
                return
            self._report_manual_failure(key, pending, error)
        await self._finish_manual_read(key)

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._pending.clear()
        self._active_keys.clear()
        self._confirmed_reads.clear()

    async def _send_mark_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int],
        max_id: int,
        *,
        origin: str = "manual",
        validate: Optional[Callable[[], Awaitable[bool]]] = None,
        chat_name: Optional[str] = None,
        topic_name: Optional[str] = None,
        queue_seconds: Optional[float] = None,
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
                await self._read_chat(input_peer, max_id)
            else:
                await self._read_topic(input_peer, topic_id, max_id)
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
                origin=origin,
                max_id=max_id,
                response=e.outcome if isinstance(e, ReadNotConfirmed) else None,
                category=type(e).__name__,
            )
            raise

        key = (chat_id, topic_id)
        self._confirmed_reads[key] = max(max_id, self._confirmed_reads.get(key, 0))
        self._confirmed_reads.move_to_end(key)
        if len(self._confirmed_reads) > MAX_CONFIRMED_READ_KEYS:
            self._confirmed_reads.popitem(last=False)
        logger.info(
            "mark_as_read_completed",
            chat_id=chat_id,
            topic_id=topic_id,
            max_id=max_id,
            origin=origin,
            queue_seconds=round(queue_seconds, 3)
            if queue_seconds is not None
            else None,
            rpc_seconds=round(time.monotonic() - started, 3),
        )
        self._emit_read(
            key,
            max_id,
            origin,
            chat_name=chat_name,
            topic_name=topic_name,
        )
        raise_if_task_stopped()
        return True

    async def _read_chat(self, input_peer: Any, max_id: int) -> None:
        result = await self.client.send_read_acknowledge(input_peer, max_id=max_id)
        raise_if_task_stopped()
        if result is not True and not isinstance(
            result, types.messages.AffectedMessages
        ):
            raise ReadNotConfirmed(response=result)

    async def _read_topic(
        self,
        input_peer: Any,
        topic_id: int,
        max_id: int,
    ) -> None:
        result = await self.client(
            functions.messages.ReadDiscussionRequest(
                peer=input_peer, msg_id=topic_id, read_max_id=max_id
            )
        )
        raise_if_task_stopped()
        if result is not True:
            raise ReadNotConfirmed(response=result)

    async def _notify(self, operation) -> None:
        try:
            deadline = asyncio.get_running_loop().time() + POST_READ_UPDATE_TIMEOUT
            await run_with_task_deadline(operation, deadline, name="read_notification")
        except Exception as error:
            logger.warning("read_notification_failed", category=type(error).__name__)

    def _emit_read(
        self, key, max_id, origin, *, chat_name=None, topic_name=None, failed=False
    ) -> None:
        dispatch_fn = self._dispatch_fn
        if self._closed or dispatch_fn is None:
            return
        chat_id, topic_id = key
        link = f"/chat/{chat_id}"
        if topic_id is not None:
            link += f"/topic/{topic_id}"
        event = SystemEvent(
            type="read_failed" if failed else "read",
            text="Read was not confirmed. Refresh before retrying."
            if failed
            else "Marked as read",
            chat_name=chat_name or f"Chat {chat_id}",
            topic_name=topic_name,
            chat_id=chat_id,
            topic_id=topic_id,
            is_read=not failed,
            read_origin=origin,
            read_through=max_id,
            link=link,
        )
        self._start_task(self._notify(lambda: dispatch_fn(event)))
