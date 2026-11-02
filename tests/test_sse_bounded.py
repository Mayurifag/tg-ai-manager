import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

from src.domain.models import Message, SystemEvent
from src.web import create_app
from src.web.sse import EventBroadcaster


def message_event(chat_id=1, topic_id=None):
    return SystemEvent(
        type="message",
        text="hello",
        chat_name="chat",
        chat_id=chat_id,
        topic_id=topic_id,
        message_model=Message(
            id=12,
            text="hello",
            date=datetime.now(),
            sender_name="<sender>",
            is_outgoing=False,
        ),
    )


async def test_slow_sse_client_is_bounded_and_other_client_keeps_up():
    broadcaster = EventBroadcaster(max_clients=2, queue_size=2)
    slow = broadcaster.subscribe()
    fast = broadcaster.subscribe()
    assert broadcaster.subscribe() is None
    for _ in range(5):
        await asyncio.wait_for(broadcaster.broadcast(message_event()), timeout=1)
        assert json.loads(fast.queue.get_nowait())["type"] == "message"
    assert slow.stale
    assert slow.queue.qsize() == 1
    assert json.loads(slow.queue.get_nowait()) == {"type": "resync_required"}
    broadcaster.unsubscribe(slow)
    assert broadcaster.subscribe() is not None


async def test_list_and_other_topic_subscribers_do_not_render_message_cards():
    broadcaster = EventBroadcaster()
    list_client = broadcaster.subscribe()
    other_topic = broadcaster.subscribe(chat_id=1, topic_id=2)
    # The broadcaster has no app context, so any attempted rendering fails.
    await broadcaster.broadcast(message_event(topic_id=3))
    for client in (list_client, other_topic):
        data = json.loads(client.queue.get_nowait())
        assert data["rendered_html"] is None
        assert data["message_model"] == {"is_outgoing": False}


async def test_matching_transcript_receives_escaped_render_with_minimal_context():
    app = create_app()
    client = app.sse.subscribe(chat_id=1, topic_id=3, debug=True)
    async with app.app_context():
        await app.sse.broadcast(message_event(topic_id=3))
    data = json.loads(client.queue.get_nowait())
    assert "&lt;sender&gt;" in data["rendered_html"]
    assert "debug-process" in data["rendered_html"]
    assert data["message_model"]["id"] == 12


async def test_edited_message_is_rendered_with_media_and_metadata_for_its_topic():
    app = create_app()
    matching = app.sse.subscribe(chat_id=1, topic_id=3)
    other = app.sse.subscribe(chat_id=1, topic_id=4)
    event = message_event(topic_id=3)
    event.type = "edited"
    event.message_model.text = "updated caption"
    event.message_model.has_media = True
    event.message_model.is_photo = True
    event.message_model.forwarded_from = "Synthetic origin"

    async with app.app_context():
        await app.sse.broadcast(event)

    data = json.loads(matching.queue.get_nowait())
    assert 'id="msg-12"' in data["rendered_html"]
    assert "/media/1/12" in data["rendered_html"]
    assert "updated caption" in data["rendered_html"]
    assert "Forwarded from Synthetic origin" in data["rendered_html"]
    assert json.loads(other.queue.get_nowait())["rendered_html"] is None


async def test_album_group_id_is_exact_in_rendered_html_and_json():
    app = create_app()
    matching = app.sse.subscribe(chat_id=1, topic_id=3)
    event = message_event(topic_id=3)
    event.message_model.grouped_id = 9223372036854775807
    async with app.app_context():
        await app.sse.broadcast(event)
    data = json.loads(matching.queue.get_nowait())
    assert data["message_model"]["grouped_id"] == "9223372036854775807"
    assert 'data-grouped-id="9223372036854775807"' in data["rendered_html"]


async def test_inflight_render_does_not_enqueue_after_account_resync(monkeypatch):
    app = create_app()
    client = app.sse.subscribe(chat_id=1, topic_id=3)
    started, release = asyncio.Event(), asyncio.Event()

    async def render_async(**_):
        started.set()
        await release.wait()
        return "synthetic old account content"

    monkeypatch.setattr(
        app.jinja_env,
        "get_template",
        lambda name: SimpleNamespace(render_async=render_async),
    )
    async with app.app_context():
        task = asyncio.create_task(app.sse.broadcast(message_event(topic_id=3)))
        await asyncio.wait_for(started.wait(), 1)
        app.sse.invalidate_clients()
        release.set()
        await asyncio.wait_for(task, 1)

    assert json.loads(client.queue.get_nowait()) == {"type": "resync_required"}
    assert client.queue.empty()


async def test_broadcasters_are_isolated_and_close_requests_resync():
    first, second = EventBroadcaster(), EventBroadcaster()
    client1, client2 = first.subscribe(), second.subscribe()
    await first.broadcast(message_event())
    assert client1.queue.qsize() == 1
    assert client2.queue.empty()
    first.close()
    assert json.loads(client1.queue.get_nowait()) == {"type": "resync_required"}
    assert first.subscribe() is None
    assert second.subscribe() is not None
