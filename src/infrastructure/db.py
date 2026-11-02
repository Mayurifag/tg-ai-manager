import sqlite3
import asyncio
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator, TypeVar

T = TypeVar("T")
_cancelled_operation: ContextVar[threading.Event | None] = ContextVar(
    "sqlite_cancelled_operation", default=None
)


class _CancelledTransaction(Exception):
    pass


class BaseSqliteRepository:
    """
    Base repository handling SQLite connection creation and
    async execution offloading.
    """

    def __init__(self, db_path: str = "data.db"):
        self.db_path = db_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Own a connection and its transaction, including on failure."""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys = ON;")
            with conn:
                yield conn
                cancelled = _cancelled_operation.get()
                if cancelled is not None and cancelled.is_set():
                    raise _CancelledTransaction()
        finally:
            conn.close()

    async def _execute(self, func: Callable[[], T]) -> T:
        """Run off-loop and wait for rollback before returning cancellation.

        Cancellation noticed before the transaction's commit boundary rolls
        it back. A commit already underway may finish; in either case the
        worker must finish before account lifecycle locks are released.
        """
        cancelled = threading.Event()
        context_token = _cancelled_operation.set(cancelled)
        try:
            task = asyncio.create_task(asyncio.to_thread(func))
        finally:
            _cancelled_operation.reset(context_token)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if task.done() and not task.cancelled():
                task.exception()  # Retrieve a worker-side rollback/error.
            raise
