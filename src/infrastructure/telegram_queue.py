import asyncio
from collections.abc import Awaitable, Callable

from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

MAX_PENDING_WRITES = 256
MANUAL_WRITE_RESERVE = 32


class WriteQueueFull(RuntimeError):
    """The operation was not accepted; callers may retry later."""


class TelegramWriteQueue:
    """Serial write queue for Telegram API operations.

    Enqueued coroutines are executed one at a time by a single worker.
    A write must manage its own retries without holding the worker during delays.
    Dropping pending operations on shutdown is acceptable (in-memory only).
    """

    def __init__(
        self,
        max_pending: int = MAX_PENDING_WRITES,
        manual_reserve: int = MANUAL_WRITE_RESERVE,
    ) -> None:
        if max_pending <= 0 or not 0 <= manual_reserve < max_pending:
            raise ValueError("Invalid write queue capacity")
        self._queue: asyncio.Queue[Callable[[], Awaitable[None]]] = asyncio.Queue(
            max_pending
        )
        self._automatic_limit = max_pending - manual_reserve
        self._worker_task: asyncio.Task[None] | None = None
        self._running = False
        self._closed = False

    async def enqueue(
        self, coro_fn: Callable[[], Awaitable[None]], *, automatic: bool = False
    ) -> None:
        """Accept immediately or reject, reserving some capacity for manual work."""
        if self._closed:
            raise RuntimeError("Write queue is closed")
        if self._queue.full() or (
            automatic and self._queue.qsize() >= self._automatic_limit
        ):
            logger.warning(
                "write_queue_full", automatic=automatic, pending=self._queue.qsize()
            )
            raise WriteQueueFull("Telegram write queue is full; try again later")
        self._queue.put_nowait(coro_fn)

    def queue_size(self) -> int:
        """Current number of pending operations."""
        return self._queue.qsize()

    async def start(self) -> None:
        """Start the background worker. Call after the Telegram client connects."""
        if self._closed:
            raise RuntimeError("Write queue is closed")
        if self.is_running():
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker())
        logger.info("write_queue_started")

    def is_running(self) -> bool:
        return bool(
            self._running and self._worker_task and not self._worker_task.done()
        )

    def retire(self) -> None:
        """Stop accepting work and cancel the worker without awaiting cleanup."""
        if self._closed:
            return
        self._closed = True
        self._running = False
        if self._worker_task is not None:
            self._worker_task.cancel()

    async def stop(self) -> None:
        """Stop the background worker. Pending operations are dropped."""
        self.retire()
        if self._worker_task is not None:
            # gather distinguishes the worker's expected cancellation from
            # cancellation of the caller, which must abort account changes.
            (result,) = await asyncio.gather(self._worker_task, return_exceptions=True)
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                raise result
            self._worker_task = None
        dropped = 0
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
            dropped += 1
        logger.info("write_queue_stopped", dropped=dropped)

    async def _worker(self) -> None:
        while self._running:
            coro_fn = await self._queue.get()
            try:
                await coro_fn()
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                logger.warning("write_queue_operation_cancelled")
            except Exception as e:
                logger.error("write_queue_operation_failed", category=type(e).__name__)
            finally:
                self._queue.task_done()
