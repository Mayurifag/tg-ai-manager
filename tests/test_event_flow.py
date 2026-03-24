"""Integration tests for the EventBus dispatch chain.

Verifies that SystemEvent instances dispatched through EventBus reach
subscribers in the expected order, with error isolation, and that
an asyncio.Queue subscriber correctly models the SSE path — all without
a real Telegram connection or Quart app context.
"""

import asyncio
from datetime import datetime

from src.domain.models import SystemEvent
from src.infrastructure.event_bus import EventBus


def _make_event(**kwargs) -> SystemEvent:
    """Build a minimal SystemEvent with sensible defaults."""
    return SystemEvent(
        type=kwargs.get("type", "message"),
        text=kwargs.get("text", "hello"),
        chat_name=kwargs.get("chat_name", "test-chat"),
    )


async def test_subscriber_receives_dispatched_event():
    """A subscribed coroutine is called with the dispatched SystemEvent."""
    bus = EventBus()
    received: list[SystemEvent] = []

    async def collector(event: SystemEvent) -> None:
        received.append(event)

    bus.subscribe(collector)
    event = _make_event(type="message", text="ping", chat_name="general")
    await bus.dispatch(event)

    assert len(received) == 1
    assert received[0].type == "message"
    assert received[0].text == "ping"
    assert received[0].chat_name == "general"


async def test_dispatch_preserves_event_fields():
    """All optional fields on a SystemEvent survive the dispatch round-trip."""
    bus = EventBus()
    received: list[SystemEvent] = []

    async def collector(event: SystemEvent) -> None:
        received.append(event)

    bus.subscribe(collector)
    now = datetime(2024, 1, 15, 12, 0, 0)
    event = SystemEvent(
        type="edited",
        text="updated text",
        chat_name="my-group",
        topic_name="dev",
        date=now,
        chat_id=42,
        topic_id=7,
        link="https://t.me/c/42/7",
        is_read=True,
    )
    await bus.dispatch(event)

    assert len(received) == 1
    evt = received[0]
    assert evt.type == "edited"
    assert evt.topic_name == "dev"
    assert evt.chat_id == 42
    assert evt.topic_id == 7
    assert evt.link == "https://t.me/c/42/7"
    assert evt.is_read is True
    assert evt.date == now


async def test_subscribers_called_in_registration_order():
    """First-registered subscriber fires before the second."""
    bus = EventBus()
    order: list[str] = []

    async def first(event: SystemEvent) -> None:
        order.append("first")

    async def second(event: SystemEvent) -> None:
        order.append("second")

    bus.subscribe(first)
    bus.subscribe(second)
    await bus.dispatch(_make_event())

    assert order == ["first", "second"]


async def test_error_in_subscriber_does_not_block_subsequent_subscribers():
    """If the first subscriber raises, the second still receives the event."""
    bus = EventBus()
    reached: list[str] = []

    async def bad_subscriber(event: SystemEvent) -> None:
        raise RuntimeError("simulated subscriber failure")

    async def good_subscriber(event: SystemEvent) -> None:
        reached.append("ok")

    bus.subscribe(bad_subscriber)
    bus.subscribe(good_subscriber)

    # Must not raise at the call site
    await bus.dispatch(_make_event())

    assert reached == ["ok"]


async def test_no_subscribers_dispatch_is_safe():
    """Dispatching with no subscribers registered does not raise."""
    bus = EventBus()
    # Should complete without error
    await bus.dispatch(_make_event())


async def test_multiple_events_delivered_to_single_subscriber():
    """Each dispatched event is delivered independently."""
    bus = EventBus()
    received: list[SystemEvent] = []

    async def collector(event: SystemEvent) -> None:
        received.append(event)

    bus.subscribe(collector)

    events = [_make_event(text=f"msg-{i}") for i in range(5)]
    for evt in events:
        await bus.dispatch(evt)

    assert len(received) == 5
    for i, evt in enumerate(received):
        assert evt.text == f"msg-{i}"


async def test_sse_queue_subscriber_receives_event():
    """Simulate the SSE path: an asyncio.Queue wired as a subscriber receives the event."""
    bus = EventBus()
    sse_queue: asyncio.Queue[SystemEvent] = asyncio.Queue()

    async def sse_handler(event: SystemEvent) -> None:
        await sse_queue.put(event)

    bus.subscribe(sse_handler)

    event = _make_event(type="message", text="sse-test", chat_name="notifications")
    await bus.dispatch(event)

    assert sse_queue.qsize() == 1
    item = sse_queue.get_nowait()
    assert item is event
    assert item.text == "sse-test"


async def test_sse_queue_receives_exactly_one_event_per_dispatch():
    """Each dispatch puts exactly one item on the SSE queue — no duplicates."""
    bus = EventBus()
    sse_queue: asyncio.Queue[SystemEvent] = asyncio.Queue()

    async def sse_handler(event: SystemEvent) -> None:
        await sse_queue.put(event)

    bus.subscribe(sse_handler)

    for i in range(3):
        await bus.dispatch(_make_event(text=f"evt-{i}"))

    assert sse_queue.qsize() == 3
    texts = [sse_queue.get_nowait().text for _ in range(3)]
    assert texts == ["evt-0", "evt-1", "evt-2"]
