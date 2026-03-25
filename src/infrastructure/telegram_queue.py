import asyncio
from collections.abc import Awaitable, Callable

from telethon.errors import FloodWaitError

from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class TelegramWriteQueue:
    """Serial write queue for Telegram API operations.

    Enqueued coroutines are executed one at a time by a single worker.
    On FloodWait (429) errors the same operation is re-queued after waiting
    the required number of seconds.
    Dropping pending operations on shutdown is acceptable (in-memory only).
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Callable[[], Awaitable[None]]] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._running = False

    async def enqueue(self, coro_fn: Callable[[], Awaitable[None]]) -> None:
        """Add a coroutine factory to the queue. Returns immediately."""
        await self._queue.put(coro_fn)

    def queue_size(self) -> int:
        """Current number of pending operations."""
        return self._queue.qsize()

    async def start(self) -> None:
        """Start the background worker. Call after the Telegram client connects."""
        self._running = True
        self._worker_task = asyncio.create_task(self._worker())
        logger.info("write_queue_started")

    async def stop(self) -> None:
        """Stop the background worker. Pending operations are dropped."""
        self._running = False
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        logger.info("write_queue_stopped", pending=self._queue.qsize())

    async def _worker(self) -> None:
        while self._running:
            try:
                coro_fn = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            requeue = False
            try:
                await coro_fn()
            except FloodWaitError as e:
                logger.warning("flood_wait_backoff", seconds=e.seconds)
                await asyncio.sleep(e.seconds)
                requeue = True
            except Exception as e:
                logger.error("write_queue_operation_failed", error=repr(e))
            finally:
                self._queue.task_done()

            if requeue:
                await self._queue.put(coro_fn)
