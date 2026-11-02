"""Tests for TelegramWriteQueue."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon import errors, types

from src.adapters.telegram.write_ops import WriteOps
import src.infrastructure.telegram_queue as tq_module
from src.infrastructure.telegram_queue import TelegramWriteQueue, WriteQueueFull


async def test_reaction_toggle_preserves_other_chosen_reactions():
    current = types.MessageReactions(
        results=[
            types.ReactionCount(
                reaction=types.ReactionEmoji(emoticon="👍"), count=1, chosen_order=0
            ),
            types.ReactionCount(
                reaction=types.ReactionCustomEmoji(document_id=123),
                count=1,
                chosen_order=1,
            ),
        ]
    )
    client = AsyncMock()
    client.get_entity.return_value = "peer"
    client.get_messages.side_effect = [[SimpleNamespace(reactions=current)], []]

    async def execute(operation):
        await operation()

    queue = SimpleNamespace(enqueue=AsyncMock(side_effect=execute))
    ops = WriteOps(client, MagicMock(), queue, None, AsyncMock())
    await ops.send_reaction(1, 2, "👍")
    request = client.await_args.args[0]
    assert len(request.reaction) == 1
    assert request.reaction[0].document_id == 123


async def test_automatic_reaction_is_desired_state_not_toggle():
    current = types.MessageReactions(
        results=[
            types.ReactionCount(
                reaction=types.ReactionEmoji(emoticon="👍"), count=1, chosen_order=0
            )
        ]
    )
    client = AsyncMock()
    client.get_entity.return_value = "peer"
    client.get_messages.return_value = [SimpleNamespace(reactions=current)]
    queue = SimpleNamespace(items=[])

    async def enqueue(operation, *, automatic=False):
        queue.items.append(operation)

    queue.enqueue = enqueue
    ops = WriteOps(client, MagicMock(), queue, None, AsyncMock())
    validate = AsyncMock(return_value=True)
    complete = AsyncMock()
    assert await ops.send_automatic_reaction(1, 1, "👍", validate, complete)
    assert not await ops.send_automatic_reaction(1, 1, "👍", validate, complete)
    await queue.items.pop()()
    client.assert_not_awaited()
    complete.assert_awaited_once_with(True)


async def test_automatic_reaction_is_dropped_if_rule_was_disabled():
    client = AsyncMock()
    client.get_entity.return_value = "peer"
    client.get_messages.return_value = [SimpleNamespace(reactions=None)]
    queue = SimpleNamespace(items=[])

    async def enqueue(operation, *, automatic=False):
        queue.items.append(operation)

    queue.enqueue = enqueue
    ops = WriteOps(client, MagicMock(), queue, None, AsyncMock())
    validate = AsyncMock(return_value=False)
    complete = AsyncMock()
    assert await ops.send_automatic_reaction(1, 1, "👍", validate, complete)
    await queue.items.pop()()
    client.get_entity.assert_not_awaited()
    client.assert_not_awaited()
    complete.assert_awaited_once_with(False)


async def test_reaction_flood_wait_reaches_write_queue():
    client = AsyncMock(side_effect=errors.FloodWaitError(request=None, capture=3))
    client.get_entity.return_value = "peer"
    client.get_messages.return_value = [SimpleNamespace(reactions=None)]

    async def execute(operation):
        await operation()

    queue = SimpleNamespace(enqueue=AsyncMock(side_effect=execute))
    ops = WriteOps(client, MagicMock(), queue, None, AsyncMock())
    with pytest.raises(errors.FloodWaitError):
        await ops.send_reaction(1, 2, "👍")


async def test_enqueued_ops_run_in_order():
    """Operations must execute in FIFO order."""
    results = []
    queue = TelegramWriteQueue()
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


async def test_flood_wait_retries_operation():
    """FloodWaitError causes the operation to be re-queued and retried."""

    class FakeFloodWait(Exception):
        seconds = 0  # instant backoff for test speed

    results = []
    call_count = [0]

    queue = TelegramWriteQueue()
    await queue.start()

    async def _op():
        call_count[0] += 1
        if call_count[0] == 1:
            raise FakeFloodWait()
        results.append("ok")

    with patch.object(tq_module, "FloodWaitError", FakeFloodWait):
        await queue.enqueue(_op)
        await asyncio.sleep(0.3)

    await queue.stop()
    assert call_count[0] == 2
    assert results == ["ok"]


async def test_failing_op_does_not_crash_worker():
    """A failing operation logs the error but the worker continues processing."""
    results = []
    queue = TelegramWriteQueue()
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
    queue = TelegramWriteQueue()
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
    queue = TelegramWriteQueue()
    await queue.start()
    await queue.stop()
    await queue.stop()  # second stop should be a no-op


async def test_reconnecting_does_not_start_duplicate_write_workers():
    queue = TelegramWriteQueue()
    await queue.start()
    original_worker = queue._worker_task
    await queue.start()
    assert queue._worker_task is original_worker
    assert queue.is_running()
    await queue.stop()
    assert not queue.is_running()


async def test_enqueue_before_start_drains_after_start():
    """Items enqueued before start() are processed after the worker launches."""
    results = []
    queue = TelegramWriteQueue()

    async def _op():
        results.append(1)

    # Enqueue without starting — asyncio.Queue is unbounded, put() won't block
    await queue.enqueue(_op)

    await queue.start()
    await asyncio.sleep(0.1)
    await queue.stop()

    assert results == [1]


async def test_stop_drops_pending_work_and_closes_queue():
    queue = TelegramWriteQueue()
    started = asyncio.Event()
    blocker = asyncio.Event()
    ran = []

    async def blocking():
        started.set()
        await blocker.wait()

    async def pending():
        ran.append(True)

    await queue.start()
    await queue.enqueue(blocking)
    await asyncio.wait_for(started.wait(), 1)
    await queue.enqueue(pending)
    await queue.stop()
    assert queue.queue_size() == 0
    await asyncio.wait_for(queue._queue.join(), 1)
    assert not ran
    with pytest.raises(RuntimeError):
        await queue.enqueue(pending)
    with pytest.raises(RuntimeError):
        await queue.start()


async def test_queue_limits_automatic_work_and_reserves_manual_capacity():
    queue = TelegramWriteQueue(max_pending=3, manual_reserve=1)
    op = AsyncMock()
    await queue.enqueue(op, automatic=True)
    await queue.enqueue(op, automatic=True)
    with pytest.raises(WriteQueueFull):
        await queue.enqueue(op, automatic=True)
    await queue.enqueue(op)
    with pytest.raises(WriteQueueFull):
        await queue.enqueue(op)
    assert queue.queue_size() == 3
    await queue.stop()
    await asyncio.wait_for(queue._queue.join(), 1)


async def test_flood_retry_does_not_deadlock_when_pending_queue_is_full():
    class FakeFloodWait(Exception):
        seconds = 0

    queue = TelegramWriteQueue(max_pending=1, manual_reserve=0)
    first_attempt = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    finished = []

    async def retry():
        nonlocal calls
        calls += 1
        if calls == 1:
            first_attempt.set()
            await release.wait()
            raise FakeFloodWait()
        finished.append("retry")

    async def manual():
        finished.append("manual")

    with patch.object(tq_module, "FloodWaitError", FakeFloodWait):
        await queue.start()
        await queue.enqueue(retry, automatic=True)
        await asyncio.wait_for(first_attempt.wait(), 1)
        await queue.enqueue(manual)
        release.set()
        await asyncio.wait_for(queue._queue.join(), 1)
        await queue.stop()
    assert finished == ["retry", "manual"]
