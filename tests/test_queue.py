"""Tests for TelegramWriteQueue."""

import asyncio
import time

import pytest

from src.infrastructure.telegram_queue import TelegramWriteQueue


async def test_enqueued_ops_run_in_order():
    """Operations must execute in FIFO order."""
    results = []
    queue = TelegramWriteQueue(delay=0)
    await queue.start()

    for i in range(5):
        val = i

        async def _op(v=val):
            results.append(v)

        await queue.enqueue(_op)

    # Give worker time to drain
    await asyncio.sleep(0.1)
    await queue.stop()

    assert results == [0, 1, 2, 3, 4]


async def test_delay_between_ops():
    """Worker sleeps ~delay seconds between consecutive operations."""
    timestamps = []
    delay = 0.1
    queue = TelegramWriteQueue(delay=delay)
    await queue.start()

    for _ in range(3):

        async def _op():
            timestamps.append(time.monotonic())

        await queue.enqueue(_op)

    await asyncio.sleep(delay * 5)
    await queue.stop()

    assert len(timestamps) == 3
    # Each gap should be at least (delay * 0.8) to tolerate scheduling jitter
    for i in range(1, len(timestamps)):
        gap = timestamps[i] - timestamps[i - 1]
        assert gap >= delay * 0.8, f"Gap {gap:.3f}s is less than {delay * 0.8:.3f}s"


async def test_failing_op_does_not_crash_worker():
    """A failing operation logs the error but the worker continues processing."""
    results = []
    queue = TelegramWriteQueue(delay=0)
    await queue.start()

    async def _bad():
        raise RuntimeError("simulated failure")

    async def _good():
        results.append("ok")

    await queue.enqueue(_bad)
    await queue.enqueue(_good)

    await asyncio.sleep(0.1)
    await queue.stop()

    # Worker must still process the good op after the bad one
    assert results == ["ok"]


async def test_queue_size_reflects_pending_ops():
    """queue_size() reports the number of unprocessed items."""
    queue = TelegramWriteQueue(delay=10)  # large delay — worker won't drain fast
    await queue.start()

    done = asyncio.Event()

    # First op blocks the worker (waits for done event)
    async def _blocking():
        await asyncio.wait_for(done.wait(), timeout=2.0)

    await queue.enqueue(_blocking)
    await asyncio.sleep(0.05)  # let worker pick up _blocking

    for _ in range(3):

        async def _noop():
            pass

        await queue.enqueue(_noop)

    # 3 items still pending
    assert queue.queue_size() == 3

    done.set()
    await asyncio.sleep(0.2)
    await queue.stop()


async def test_stop_is_idempotent():
    """Calling stop() on an already-stopped queue does not raise."""
    queue = TelegramWriteQueue(delay=0)
    await queue.start()
    await queue.stop()
    await queue.stop()  # second stop should be a no-op


async def test_enqueue_before_start_drains_after_start():
    """Items enqueued before start() are processed after the worker launches."""
    results = []
    queue = TelegramWriteQueue(delay=0)

    async def _op():
        results.append(1)

    # Enqueue without starting — asyncio.Queue is unbounded, put() won't block
    await queue.enqueue(_op)

    await queue.start()
    await asyncio.sleep(0.1)
    await queue.stop()

    assert results == [1]
