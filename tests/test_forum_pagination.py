from datetime import datetime, timedelta, timezone
from html import unescape
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon import errors, functions, types

from src.adapters.telegram.forum_ops import ForumOps
from src.domain.errors import TelegramUnavailable
from src.domain.models import Chat, ChatType, ForumTopicCursor, ForumTopicPage
from src.users.models import User
from src.web import create_app


def topic(topic_id, unread=1):
    return SimpleNamespace(
        id=topic_id,
        title=f"Topic {topic_id}",
        unread_count=unread,
        top_message=topic_id + 1000,
        date=datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=topic_id),
    )


def page(topics, *, order_by_create_date=False):
    return SimpleNamespace(
        topics=topics,
        messages=[
            SimpleNamespace(id=t.top_message, date=t.date + timedelta(days=1))
            for t in topics
            if not isinstance(t, types.ForumTopicDeleted)
        ],
        order_by_create_date=order_by_create_date,
    )


async def test_unread_topics_paginate_short_pages_and_deduplicate():
    first = [topic(i) for i in range(1, 101)]
    second = [topic(i) for i in range(100, 121)]
    client = AsyncMock(side_effect=[page(first), page(second), page([])])
    client.get_entity.return_value = "forum"
    result = await ForumOps(client).get_unread_topics(-100)
    assert {t.id for t in result} == set(range(1, 121))
    second_request = client.await_args_list[1].args[0]
    assert isinstance(second_request, functions.messages.GetForumTopicsRequest)
    assert (second_request.offset_topic, second_request.offset_id) == (100, 1100)
    assert second_request.offset_date == first[-1].date + timedelta(days=1)
    assert client.await_count == 3


async def test_created_date_order_and_deleted_topics_use_last_valid_cursor():
    current = topic(7)
    client = AsyncMock(
        side_effect=[
            page([current, types.ForumTopicDeleted(id=8)], order_by_create_date=True),
            page([]),
        ]
    )
    client.get_entity.return_value = "forum"
    result = await ForumOps(client).get_unread_topics(-100)
    assert [t.id for t in result] == [7]
    request = client.await_args_list[1].args[0]
    assert request.offset_topic == 7
    assert request.offset_date == current.date


async def test_configured_topic_ids_use_bounded_direct_requests():
    client = AsyncMock(
        side_effect=[page([topic(1), topic(99, unread=0)]), page([topic(101)])]
    )
    client.get_entity.return_value = "forum"
    result = await ForumOps(client).get_unread_topics(-100, list(range(1, 102)) + [1])
    assert [t.id for t in result] == [1, 101]
    assert all(
        isinstance(call.args[0], functions.messages.GetForumTopicsByIDRequest)
        for call in client.await_args_list
    )
    assert [len(call.args[0].topics) for call in client.await_args_list] == [100, 1]


async def test_repeated_cursor_and_flood_wait_do_not_silently_succeed():
    current = page([topic(1)])
    client = AsyncMock(side_effect=[current, current])
    client.get_entity.return_value = "forum"
    with pytest.raises(RuntimeError, match="did not advance"):
        await ForumOps(client).get_unread_topics(-100)
    client.side_effect = errors.FloodWaitError(request=None, capture=12)
    with pytest.raises(errors.FloodWaitError):
        await ForumOps(client).get_unread_topics(-100)


async def test_forum_view_error_is_not_reported_as_an_empty_forum():
    client = AsyncMock(side_effect=errors.FloodWaitError(request=None, capture=12))
    client.get_entity.return_value = "forum"
    with pytest.raises(TelegramUnavailable) as exc:
        await ForumOps(client).get_forum_topics(-100)
    assert exc.value.retry_after == 12


async def test_topic_pages_use_telegram_offsets_past_first_twenty():
    first = [topic(i) for i in range(1, 21)]
    second = [topic(i) for i in range(21, 26)]
    client = AsyncMock(side_effect=[page(first), page(second), page([])])
    client.get_entity.return_value = "forum"
    ops = ForumOps(client)

    first_page = await ops.get_forum_topics_page(-100)
    second_page = await ops.get_forum_topics_page(-100, first_page.next_cursor)
    final_page = await ops.get_forum_topics_page(-100, second_page.next_cursor)

    assert [item.id for item in first_page.topics] == list(range(1, 21))
    assert [item.id for item in second_page.topics] == list(range(21, 26))
    request = client.await_args_list[1].args[0]
    assert isinstance(request, functions.messages.GetForumTopicsRequest)
    assert (request.offset_id, request.offset_topic) == (1020, 20)
    assert request.offset_date == first[-1].date + timedelta(days=1)
    assert final_page.topics == [] and final_page.next_cursor is None


async def test_topic_card_lookup_uses_id_not_first_page():
    client = AsyncMock(return_value=page([topic(25)]))
    client.get_entity.return_value = "forum"
    result = await ForumOps(client).get_forum_topic(-100, 25)
    assert result.id == 25
    request = client.await_args.args[0]
    assert isinstance(request, functions.messages.GetForumTopicsByIDRequest)
    assert request.topics == [25]


async def test_topic_names_are_bounded_and_refresh_after_rename_or_expiry(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(
        "src.adapters.telegram.forum_ops.time",
        SimpleNamespace(monotonic=lambda: now[0]),
    )
    before, renamed, expired = topic(7), topic(7), topic(7)
    before.title, renamed.title, expired.title = "Before", "Renamed", "After expiry"
    client = AsyncMock(side_effect=[page([before]), page([renamed]), page([expired])])
    client.get_entity.return_value = "forum"
    ops = ForumOps(client)
    assert await ops.get_topic_name(-100, 7) == "Before"
    assert await ops.get_topic_name(-100, 7) == "Before"
    assert client.await_count == 1
    ops.invalidate_topic_name(-100, 7)
    assert await ops.get_topic_name(-100, 7) == "Renamed"
    assert client.await_count == 2
    now[0] += 61
    assert await ops.get_topic_name(-100, 7) == "After expiry"
    assert client.await_count == 3
    assert isinstance(
        client.await_args.args[0], functions.messages.GetForumTopicsByIDRequest
    )


async def test_topic_name_cache_has_a_fixed_capacity(monkeypatch):
    monkeypatch.setattr("src.adapters.telegram.forum_ops.MAX_TOPIC_NAMES", 2)
    client = AsyncMock(
        side_effect=[page([topic(1)]), page([topic(2)]), page([topic(3)])]
    )
    client.get_entity.return_value = "forum"
    ops = ForumOps(client)
    for topic_id in (1, 2, 3):
        assert await ops.get_topic_name(-100, topic_id) == f"Topic {topic_id}"
    assert list(ops._topic_name_cache) == [(-100, 2), (-100, 3)]


async def test_forum_page_links_reach_later_topics_and_validate_cursor():
    app = create_app()
    app.tg_adapter = SimpleNamespace(is_connected=lambda: True)
    app.user_repo = SimpleNamespace(get_user=AsyncMock(return_value=User()))
    forum = Chat(id=-100, name="Forum", unread_count=0, type=ChatType.FORUM)
    cursor = ForumTopicCursor(datetime(2025, 1, 1, tzinfo=timezone.utc), 1020, 20)
    first = ForumTopicPage(
        [Chat(id=20, name="First page", unread_count=0, type=ChatType.TOPIC)], cursor
    )
    second = ForumTopicPage(
        [Chat(id=25, name="Later page", unread_count=0, type=ChatType.TOPIC)], None
    )
    app.chat_interactor = MagicMock()
    app.chat_interactor.get_chat = AsyncMock(return_value=forum)
    app.chat_interactor.get_forum_topics_page = AsyncMock(side_effect=[first, second])
    client = app.test_client()

    response = await client.get("/forum/-100")
    assert response.status_code == 200
    html = await response.get_data(as_text=True)
    assert 'class="chat-card-wrapper" data-chat-id="20"' in html
    next_link = re.search(r'href="([^"]+)">Next topics</a>', html)
    assert next_link
    response = await client.get(unescape(next_link.group(1)))
    assert response.status_code == 200
    assert "Later page" in await response.get_data(as_text=True)
    assert app.chat_interactor.get_forum_topics_page.await_args.args == (-100, cursor)

    response = await client.get(
        "/forum/-100?offset_date=abc&offset_id=1&offset_topic=2"
    )
    assert response.status_code == 400
    assert app.chat_interactor.get_forum_topics_page.await_count == 2
