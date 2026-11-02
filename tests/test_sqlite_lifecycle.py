import asyncio
import threading

import pytest

from src.infrastructure.db import BaseSqliteRepository


async def test_cancelled_transaction_finishes_rollback_before_task_exits(tmp_path):
    repo = BaseSqliteRepository(str(tmp_path / "cancelled.db"))
    with repo._connect() as conn:
        conn.execute("CREATE TABLE items (value TEXT)")
        conn.execute("INSERT INTO items VALUES ('original')")

    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release, finished = threading.Event(), threading.Event()

    def replace():
        try:
            with repo._connect() as conn:
                conn.execute("DELETE FROM items")
                loop.call_soon_threadsafe(started.set)
                if not release.wait(5):
                    raise TimeoutError("synthetic test timed out")
                conn.execute("INSERT INTO items VALUES ('replacement')")
        finally:
            finished.set()

    task = asyncio.create_task(repo._execute(replace))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done(), "the underlying SQLite worker is still running"
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done(), "a second cancellation must not abandon the worker"
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 5)

    with pytest.raises(asyncio.CancelledError):
        await task
    with repo._connect() as conn:
        assert [row[0] for row in conn.execute("SELECT value FROM items")] == [
            "original"
        ]
