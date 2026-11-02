import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart
from telethon import types

from src.adapters.telegram.chat_query_ops import ChatQueryOps
from src.application import interactors
from src.application.interactors import ChatInteractor, ChatSnapshotExpired
from src.domain.errors import AccountChanged, TelegramUnavailable
from src.domain.models import (
    Chat,
    ChatType,
    ForumTopicPage,
    Message,
    Reaction,
    SystemEvent,
)
from src.web.routes.chat import chat_bp
from src.web import create_app


def make_chats(count=47):
    # Telegram returns pinned chats first, then the rest in recent order.
    return [
        Chat(
            id=i,
            name=f"Chat {i}",
            unread_count=1 if i in (1, 25, 45) else 0,
            type=ChatType.USER,
            is_pinned=i < 2,
        )
        for i in range(count)
    ]


@pytest.fixture
def interactor():
    repo = MagicMock()
    repo.get_chats = AsyncMock(return_value=make_chats())
    return ChatInteractor(repo, MagicMock(), MagicMock())


@pytest.fixture
def client(interactor):
    root = Path(__file__).resolve().parents[1]
    app = Quart(__name__, template_folder=str(root / "src/templates"))
    app.jinja_env.filters["file_mtime"] = lambda _: "1"
    app.register_blueprint(chat_bp)
    app.chat_interactor = interactor
    return app.test_client()


async def test_telegram_fetches_all_dialogs():
    dialogs = [
        SimpleNamespace(
            id=i,
            name=f"Chat {i}",
            unread_count=0,
            entity=object(),
            message=None,
            pinned=False,
            is_user=True,
        )
        for i in range(35)
    ]
    client = SimpleNamespace(get_dialogs=AsyncMock(return_value=dialogs))
    media = SimpleNamespace(_get_chat_image=AsyncMock(return_value=None))
    ops = ChatQueryOps(client, MagicMock(), media)

    chats = await ops.get_chats()

    client.get_dialogs.assert_awaited_once_with(limit=None)
    assert [chat.id for chat in chats] == list(range(35))


async def test_chat_preview_uses_dialog_response_without_an_extra_history_request():
    entity = types.User(id=42, access_hash=99, first_name="Chat")
    dialog = SimpleNamespace(unread_count=2, pinned=True, top_message=7)
    message = SimpleNamespace(id=7, message="Latest", media=None)
    client = AsyncMock(
        return_value=SimpleNamespace(dialogs=[dialog], messages=[message])
    )
    client.get_entity.return_value = entity
    media = SimpleNamespace(_get_chat_image=AsyncMock(return_value=None))
    ops = ChatQueryOps(client, MagicMock(), media)

    chat = await ops.get_chat(42)
    assert chat.last_message_preview == "Latest"
    assert chat.unread_count == 2 and chat.is_pinned
    client.get_messages.assert_not_awaited()


async def test_message_replies_already_in_page_do_not_need_another_request():
    messages = [
        SimpleNamespace(id=2, reply_to=SimpleNamespace(reply_to_msg_id=1)),
        SimpleNamespace(id=1, reply_to=None),
    ]
    client = SimpleNamespace(
        get_input_entity=AsyncMock(return_value="peer"),
        get_messages=AsyncMock(return_value=messages),
    )
    parser = MagicMock()
    parser._parse_message = AsyncMock(side_effect=lambda msg, replies, chat_id: msg)
    ops = ChatQueryOps(client, parser, MagicMock())

    result = await ops.get_messages(42)
    assert result == messages
    client.get_messages.assert_awaited_once()
    assert parser._parse_message.await_args_list[0].args[1][1] is messages[1]


async def test_newer_history_is_bounded_and_keeps_the_topic_scope():
    messages = [SimpleNamespace(id=id, reply_to=None) for id in (101, 102)]
    client = SimpleNamespace(
        get_input_entity=AsyncMock(return_value="peer"),
        get_messages=AsyncMock(return_value=messages),
    )
    parser = MagicMock()
    parser._parse_message = AsyncMock(side_effect=lambda msg, replies, chat_id: msg)
    ops = ChatQueryOps(client, parser, MagicMock())

    result = await ops.get_messages(-100, limit=20, topic_id=7, after_id=100)

    client.get_messages.assert_awaited_once_with(
        "peer", limit=20, reply_to=7, offset_id=0, ids=None, min_id=100, reverse=True
    )
    assert [message.id for message in result] == [102, 101]


def history_message(id, grouped_id=None):
    return Message(
        id=id,
        text="synthetic",
        date=datetime.now(),
        sender_name="sender",
        is_outgoing=False,
        grouped_id=grouped_id,
    )


def synthetic_history(grouped_ids):
    messages = [history_message(id, grouped_ids.get(id)) for id in range(200, 0, -1)]

    async def fetch(chat_id, limit=20, topic_id=None, offset_id=0, after_id=None):
        assert chat_id == -100 and topic_id == 7
        if after_id is not None:
            # Telethon's reverse query returns the closest newer messages first;
            # the adapter presents the result newest-first to the interactor.
            return list(
                reversed([m for m in reversed(messages) if m.id > after_id][:limit])
            )
        return [m for m in messages if not offset_id or m.id < offset_id][:limit]

    return fetch


async def test_initial_and_older_history_keep_a_boundary_album_in_one_row(interactor):
    interactor.repository.get_messages = AsyncMock(
        side_effect=synthetic_history({179: 8, 180: 8, 181: 8})
    )
    first = await interactor.get_chat_messages(-100, 7)
    album = first[-1]
    assert album.id == 181
    assert [part.id for part in album.album_parts] == [179, 180, 181]
    second = await interactor.get_chat_messages(-100, 7, offset_id=179)
    assert second[0].id == 178
    assert not any(message.grouped_id == 8 for message in second)
    assert interactor.repository.get_messages.await_args_list[1].kwargs == {
        "limit": 20,
        "topic_id": 7,
        "offset_id": 181,
    }
    interactor.repository.mark_as_read.assert_not_called()


async def test_newer_history_keeps_a_boundary_album_and_cursor(interactor):
    interactor.repository.get_messages = AsyncMock(
        side_effect=synthetic_history({180: 9, 181: 9, 182: 9})
    )
    first = await interactor.get_chat_messages(-100, 7, after_id=161)
    assert first[0].id == 182
    assert [part.id for part in first[0].album_parts] == [180, 181, 182]
    second = await interactor.get_chat_messages(-100, 7, after_id=182)
    assert second[-1].id == 183
    assert not any(message.grouped_id == 9 for message in second)
    interactor.repository.mark_as_read.assert_not_called()


async def test_reading_window_completes_both_album_boundaries(interactor):
    interactor.repository.get_messages = AsyncMock(
        side_effect=synthetic_history({131: 8, 132: 8, 171: 9, 172: 9})
    )
    window = await interactor.get_message_window(-100, 7, 151)
    assert [part.id for part in window[0].album_parts] == [171, 172]
    assert [part.id for part in window[-1].album_parts] == [131, 132]
    assert len(interactor.repository.get_messages.await_args_list) == 4


async def test_history_looks_ahead_only_near_an_album_boundary(interactor):
    interactor.repository.get_messages = AsyncMock(side_effect=synthetic_history({}))
    await interactor.get_chat_messages(-100, 7)
    assert interactor.repository.get_messages.await_count == 1

    grouped = {id: 8 for id in range(181, 191)}
    interactor.repository.get_messages = AsyncMock(
        side_effect=synthetic_history(grouped)
    )
    result = await interactor.get_chat_messages(-100, 7, offset_id=201)
    assert len(result[-1].album_parts) == 10
    assert interactor.repository.get_messages.await_count == 2


async def test_boundary_album_includes_interleaved_messages_without_a_gap(interactor):
    interactor.repository.get_messages = AsyncMock(
        side_effect=synthetic_history({182: 8, 180: 8})
    )
    first = await interactor.get_chat_messages(-100, 7)
    album = next(message for message in first if message.grouped_id == 8)
    assert [part.id for part in album.album_parts] == [180, 182]
    assert first[-1].id == 181  # The lowest ID is an album part, not the last row.
    second = await interactor.get_chat_messages(-100, 7, offset_id=180)
    assert second[0].id == 179


async def test_boundary_album_can_extend_beyond_ten_parts(interactor):
    interactor.repository.get_messages = AsyncMock(
        side_effect=synthetic_history({id: 8 for id in range(169, 191)})
    )
    first = await interactor.get_chat_messages(-100, 7)
    album = next(message for message in first if message.grouped_id == 8)
    assert [part.id for part in album.album_parts] == list(range(169, 191))
    second = await interactor.get_chat_messages(-100, 7, offset_id=169)
    assert second[0].id == 168


async def test_failed_album_boundary_fetch_does_not_return_a_partial_page(interactor):
    fetch = synthetic_history({161: 8, 160: 8})

    async def fail_extra(*args, **kwargs):
        if kwargs.get("limit") == 20 and kwargs.get("offset_id") == 161:
            raise TelegramUnavailable()
        return await fetch(*args, **kwargs)

    interactor.repository.get_messages = AsyncMock(side_effect=fail_extra)
    with pytest.raises(TelegramUnavailable):
        await interactor.get_chat_messages(-100, 7, offset_id=181)


async def test_album_boundary_fetch_rejects_history_changed_by_live_event(interactor):
    fetch = synthetic_history({161: 8, 160: 8})

    async def changed_during_extra(*args, **kwargs):
        if kwargs.get("limit") == 20 and kwargs.get("offset_id") == 161:
            await interactor.handle_event(
                SystemEvent(type="deleted", text="", chat_name="", chat_id=-100)
            )
        return await fetch(*args, **kwargs)

    interactor.repository.get_messages = AsyncMock(side_effect=changed_during_extra)
    with pytest.raises(TelegramUnavailable):
        await interactor.get_chat_messages(-100, 7, offset_id=181)


async def test_account_change_during_album_boundary_fetch_discards_old_history(
    interactor,
):
    started, release = asyncio.Event(), asyncio.Event()
    fetch = synthetic_history({161: 8, 160: 8})

    async def delayed(*args, **kwargs):
        if kwargs.get("limit") == 20 and kwargs.get("offset_id") == 161:
            started.set()
            await release.wait()
        return await fetch(*args, **kwargs)

    interactor.repository.get_messages = AsyncMock(side_effect=delayed)
    task = asyncio.create_task(interactor.get_chat_messages(-100, 7, offset_id=181))
    try:
        await asyncio.wait_for(started.wait(), 1)
        interactor.replace_repository(MagicMock())
    finally:
        release.set()
    with pytest.raises(AccountChanged):
        await task


async def test_history_window_is_bounded_scoped_and_groups_album_across_anchor(
    interactor,
):
    older = [history_message(100, 8), history_message(99)]
    newer = [history_message(102), history_message(101, 8)]
    interactor.repository.get_messages = AsyncMock(side_effect=[older, newer])

    messages = await interactor.get_message_window(-100, 7, 100)

    assert interactor.repository.get_messages.await_args_list == [
        ((-100,), {"limit": 20, "topic_id": 7, "offset_id": 101}),
        ((-100,), {"limit": 20, "topic_id": 7, "after_id": 100}),
    ]
    assert [message.id for message in messages] == [102, 101, 99]
    assert [part.id for part in messages[1].album_parts] == [100, 101]
    interactor.repository.mark_as_read.assert_not_called()


async def test_history_window_rejects_a_missing_or_wrong_topic_anchor(interactor):
    interactor.repository.get_messages = AsyncMock(return_value=[history_message(99)])
    assert await interactor.get_message_window(-100, 7, 100) is None
    interactor.repository.get_messages.assert_awaited_once()
    interactor.repository.mark_as_read.assert_not_called()


async def test_history_window_discards_old_account_results(interactor):
    started, release = asyncio.Event(), asyncio.Event()

    async def history(*args, **kwargs):
        started.set()
        await release.wait()
        return [history_message(100)]

    interactor.repository.get_messages = AsyncMock(side_effect=history)
    task = asyncio.create_task(interactor.get_message_window(-100, 7, 100))
    await asyncio.wait_for(started.wait(), 1)
    interactor.replace_repository(MagicMock())
    release.set()
    with pytest.raises(AccountChanged):
        await task


async def test_history_window_retries_if_a_message_changes_during_fetch(interactor):
    old, updated = history_message(100), history_message(100)
    updated.text = "updated"
    calls = 0

    async def history(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            await interactor.handle_event(
                SystemEvent(type="edited", text="", chat_name="", chat_id=-100)
            )
            return [old]
        if calls == 3:
            return [updated]
        return []

    interactor.repository.get_messages = AsyncMock(side_effect=history)
    result = await interactor.get_message_window(-100, 7, 100)
    assert [message.text for message in result] == ["updated"]
    assert calls == 4


async def test_history_window_handles_the_maximum_message_id_without_overflow(
    interactor,
):
    maximum = 2**31 - 1
    interactor.repository.get_messages = AsyncMock(
        return_value=[history_message(maximum)]
    )
    messages = await interactor.get_message_window(42, None, maximum)
    assert [message.id for message in messages] == [maximum]
    interactor.repository.get_messages.assert_awaited_once_with(
        42, limit=20, topic_id=None, offset_id=0
    )


async def test_history_api_renders_a_scoped_window_and_reports_a_missing_anchor(
    client, interactor
):
    interactor.get_message_window = AsyncMock(
        side_effect=[[history_message(100)], None]
    )
    response = await client.get("/api/chat/-100/history?around_id=100&topic_id=7")
    assert response.status_code == 200
    assert 'id="msg-100"' in (await response.get_json())["html"]
    interactor.get_message_window.assert_awaited_once_with(-100, 7, 100)
    response = await client.get("/api/chat/-100/history?around_id=100&topic_id=7")
    assert response.status_code == 404


async def test_history_api_fetches_newer_messages_with_the_requested_topic(
    client, interactor
):
    interactor.repository.get_messages = AsyncMock(return_value=[history_message(101)])
    response = await client.get("/api/chat/-100/history?after_id=100&topic_id=7")
    assert response.status_code == 200
    assert 'id="msg-101"' in (await response.get_json())["html"]
    interactor.repository.get_messages.assert_awaited_once_with(
        -100, topic_id=7, after_id=100
    )


async def test_album_history_exposes_reactions_for_each_member(client, interactor):
    first = history_message(101, grouped_id=8)
    second = history_message(100, grouped_id=8)
    first.is_photo = second.is_photo = True
    second.reactions = [Reaction(emoji="👍", count=2, is_chosen=False)]
    interactor.repository.get_messages = AsyncMock(return_value=[first, second])

    response = await client.get("/api/chat/-100/history?offset_id=102&topic_id=7")
    html = (await response.get_json())["html"]
    assert response.status_code == 200
    assert html.count('id="reactions-101"') == 1
    assert html.count('id="reactions-100"') == 1
    assert html.count('id="msg-101"') == 1
    assert html.count('id="msg-100"') == 1
    assert 'data-msg-id="100"' in html and 'data-msg-id="101"' in html
    assert 'aria-label="React to album item 1 with 💩"' in html
    assert 'aria-label="React to album item 2 with 💩"' in html
    assert 'aria-label="React to album item 1 with 👍"' in html


@pytest.mark.parametrize(
    "query",
    [
        "around_id=0",
        "around_id=2147483648",
        "after_id=-1",
        "after_id=0",
        "around_id=100&after_id=101",
        "around_id=100&offset_id=0",
        "after_id=100&after_id=101",
    ],
)
async def test_history_api_rejects_invalid_or_ambiguous_windows(client, query):
    response = await client.get(f"/api/chat/42/history?{query}")
    assert response.status_code == 400


async def test_telegram_history_failure_is_not_empty_history():
    client = SimpleNamespace(
        get_input_entity=AsyncMock(return_value="peer"),
        get_messages=AsyncMock(side_effect=OSError("network unavailable")),
    )
    ops = ChatQueryOps(client, MagicMock(), MagicMock())
    with pytest.raises(TelegramUnavailable):
        await ops.get_messages(42)


async def test_failed_dialog_metadata_does_not_claim_zero_unread():
    entity = types.User(id=42, access_hash=99, first_name="Chat")
    client = AsyncMock(side_effect=OSError("network unavailable"))
    client.get_entity.return_value = entity
    ops = ChatQueryOps(client, MagicMock(), MagicMock(_get_chat_image=AsyncMock()))
    with pytest.raises(TelegramUnavailable):
        await ops.get_chat(42)


async def test_unread_first_across_stable_pages(interactor):
    first = await interactor.get_chat_page()
    # Changes to the source list must not shift the boundaries of this snapshot.
    interactor.repository.get_chats.return_value.reverse()
    second = await interactor.get_chat_page(first.snapshot, first.next_offset)
    last = await interactor.get_chat_page(second.snapshot, second.next_offset)

    ids = [chat.id for page in (first, second, last) for chat in page.chats]
    assert ids[:3] == [1, 25, 45]
    assert ids[3:] == [i for i in range(47) if i not in (1, 25, 45)]
    assert len(ids) == len(set(ids)) == 47
    assert last.next_offset is None
    interactor.repository.get_chats.assert_awaited_once()


async def test_read_event_refreshes_card_without_mutating_page_snapshot(interactor):
    page = await interactor.get_chat_page()
    original = next(chat for chat in page.chats if chat.id == 1)
    fresh = Chat(id=1, name="Fresh", unread_count=0, type=ChatType.USER)
    interactor.repository.get_chat = AsyncMock(return_value=fresh)
    interactor.action_repo.add_log = AsyncMock()
    await interactor.handle_event(
        SystemEvent(
            type="read", text="done", chat_name="Chat", chat_id=1, read_origin="manual"
        )
    )
    assert await interactor.get_chat(1) is fresh
    assert original.unread_count == 1
    interactor.repository.get_chat.assert_awaited_once_with(1)
    interactor.action_repo.add_log.assert_awaited_once()


async def test_manual_read_endpoint_only_reports_queued(client, interactor):
    interactor.repository.mark_as_read = AsyncMock()
    interactor.action_repo.add_log = AsyncMock()
    response = await client.post("/api/chat/42/read", json={})
    assert response.status_code == 202
    assert (await response.get_json())["status"] == "queued"
    interactor.action_repo.add_log.assert_not_awaited()


async def test_full_write_queue_returns_retryable_error(client, interactor):
    from src.infrastructure.telegram_queue import WriteQueueFull

    interactor.repository.mark_as_read = AsyncMock(side_effect=WriteQueueFull())
    response = await client.post("/api/chat/42/read", json={})
    assert response.status_code == 503
    assert "not queued" in (await response.get_json())["error"]
    interactor.repository.send_reaction = AsyncMock(side_effect=WriteQueueFull())
    response = await client.post(
        "/api/chat/42/message/2/reaction", json={"reaction": "👍"}
    )
    assert response.status_code == 503
    assert "not queued" in (await response.get_json())["error"]


async def test_malformed_read_and_history_do_not_widen_scope(client, interactor):
    interactor.repository.mark_as_read = AsyncMock()
    response = await client.post("/api/chat/42/read", json={"topic": 3})
    assert response.status_code == 400
    interactor.repository.mark_as_read.assert_not_awaited()
    response = await client.get("/api/chat/42/history?topic_id=invalid")
    assert response.status_code == 400
    response = await client.get("/api/chat/42/history?offset_id=bad")
    assert response.status_code == 400


@pytest.mark.parametrize("count, next_offset", [(0, None), (20, None), (21, 20)])
async def test_page_boundaries(interactor, count, next_offset):
    interactor.repository.get_chats.return_value = make_chats(count)
    first = await interactor.get_chat_page()
    assert len(first.chats) == min(count, 20)
    assert first.next_offset == next_offset
    if next_offset is not None:
        last = await interactor.get_chat_page(first.snapshot, next_offset)
        assert len(last.chats) == 1
        assert last.next_offset is None


async def test_snapshot_expiration_and_lru_limit(interactor, monkeypatch):
    clock = [0]
    monkeypatch.setattr(interactors.time, "monotonic", lambda: clock[0])
    first = await interactor.get_chat_page()
    for _ in range(interactors.MAX_CHAT_SNAPSHOTS):
        await interactor.get_chat_page()
    with pytest.raises(ChatSnapshotExpired):
        await interactor.get_chat_page(first.snapshot, first.next_offset)

    current = await interactor.get_chat_page()
    clock[0] += interactors.CHAT_SNAPSHOT_TTL
    with pytest.raises(ChatSnapshotExpired):
        await interactor.get_chat_page(current.snapshot, current.next_offset)


async def test_switching_accounts_discards_old_snapshots(interactor):
    page = await interactor.get_chat_page()
    interactor.replace_repository(MagicMock())
    with pytest.raises(ChatSnapshotExpired):
        await interactor.get_chat_page(page.snapshot, page.next_offset)


async def test_page_routes_render_consistent_card_metadata(client):
    response = await client.get("/")
    html = await response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'id="chat-list-container" aria-busy="false"' in html
    assert 'id="refresh-chats"' not in html
    assert html.index('data-chat-id="45"') < html.index('data-chat-id="0"')
    assert "chat-skeleton" not in html

    first = await (await client.get("/api/chats")).get_json()
    assert first["html"].index('data-chat-id="45"') < first["html"].index(
        'data-chat-id="0"'
    )
    assert 'data-chat-id="1" data-is-unread="true"' in first["html"]
    assert 'data-is-pinned="true"' in first["html"]
    assert first["next_offset"] == 20
    second_response = await client.get(
        f"/api/chats?snapshot={first['snapshot']}&offset={first['next_offset']}"
    )
    second = await second_response.get_json()
    assert second_response.status_code == 200
    assert second["next_offset"] == 40
    assert 'data-chat-id="18"' in second["html"]
    assert 'data-chat-id="45"' not in second["html"]


async def test_index_renders_warm_dialogs_without_another_telegram_scan(
    client, interactor
):
    await interactor.get_chat_page()
    assert (await client.get("/")).status_code == 200
    interactor.repository.get_chats.assert_awaited_once()


async def test_background_warms_recent_histories_and_reuses_them(interactor):
    from datetime import datetime

    interactor.repository.is_connected.return_value = True
    interactor.repository.get_chats.return_value = make_chats(2)
    interactor.repository.get_messages = AsyncMock(
        return_value=[
            Message(
                id=7,
                text="ready",
                date=datetime.now(),
                sender_name="A",
                is_outgoing=False,
            )
        ]
    )
    stop = asyncio.Event()
    warmer = asyncio.create_task(interactor.run_cache_warmer(stop))
    try:

        async def wait_until_warm():
            while interactor.repository.get_messages.await_count < 2:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_until_warm(), timeout=2)
        await interactor._prefetch_queue.join()
        assert (await interactor.get_chat_messages(1))[0].text == "ready"
        assert interactor.repository.get_messages.await_count == 2
        assert (await interactor.get_chat(1)).id == 1
        interactor.repository.get_chat.assert_not_called()
    finally:
        stop.set()
        await warmer


async def test_prefetch_worker_survives_a_cancelled_shared_download(interactor):
    first, second = make_chats(2)
    first.image_url = "/media/avatar/0"
    interactor.repository.get_chat_avatar = AsyncMock(
        side_effect=asyncio.CancelledError
    )
    interactor.repository.get_messages = AsyncMock(return_value=[])
    interactor._queue_prefetch([first, second])
    worker = asyncio.create_task(interactor._prefetch_worker())
    try:
        await asyncio.wait_for(interactor._prefetch_queue.join(), timeout=2)
        assert not worker.done()
        interactor.repository.get_messages.assert_awaited_once_with(
            second.id, topic_id=None, offset_id=0
        )
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_inflight_history_is_refetched_after_new_message(interactor):
    from datetime import datetime

    started, finish = asyncio.Event(), asyncio.Event()
    old = Message(
        id=1, text="old", date=datetime.now(), sender_name="A", is_outgoing=False
    )
    new = Message(
        id=2, text="new", date=datetime.now(), sender_name="A", is_outgoing=False
    )

    async def fetch(*args, **kwargs):
        if interactor.repository.get_messages.await_count == 1:
            started.set()
            await finish.wait()
            return [old]
        return [new]

    interactor.repository.get_messages = AsyncMock(side_effect=fetch)
    task = asyncio.create_task(interactor.get_chat_messages(42))
    await started.wait()
    await interactor.handle_event(
        SystemEvent(type="message", text="new", chat_name="A", chat_id=42)
    )
    finish.set()
    assert (await task)[0].id == 2
    assert (await interactor.get_chat_messages(42))[0].id == 2
    assert interactor.repository.get_messages.await_count == 2


async def test_reaction_updates_patch_cached_album_without_refetch(interactor):
    from datetime import datetime

    first = Message(
        id=2,
        text="caption",
        date=datetime.now(),
        sender_name="A",
        is_outgoing=False,
        grouped_id=10,
    )
    part = Message(
        id=1,
        text="",
        date=datetime.now(),
        sender_name="A",
        is_outgoing=False,
        grouped_id=10,
    )
    interactor.repository.get_messages = AsyncMock(return_value=[first, part])
    initial = await interactor.get_chat_messages(42)
    for count in range(1, 101):
        partial = Message(
            id=1,
            text="",
            date=datetime.now(),
            sender_name="",
            is_outgoing=False,
            reactions=[Reaction("👍", count, True)],
        )
        await interactor.handle_event(
            SystemEvent(
                type="reaction_update",
                text="",
                chat_name="",
                chat_id=42,
                message_model=partial,
            )
        )
    result = await interactor.get_chat_messages(42)
    assert result[0].text == "caption"
    assert result[0].album_parts[0].reactions[0].count == 100
    assert result[0].reactions == []
    assert initial[0].album_parts[0].reactions == []
    interactor.repository.get_messages.assert_awaited_once()


async def test_reaction_update_invalidates_inflight_history_version(interactor):
    from datetime import datetime

    started, finish = asyncio.Event(), asyncio.Event()
    old = Message(
        id=1, text="old", date=datetime.now(), sender_name="A", is_outgoing=False
    )
    fresh = replace(old, reactions=[Reaction("👍", 1, True)])

    async def fetch(*args, **kwargs):
        if interactor.repository.get_messages.await_count == 1:
            started.set()
            await finish.wait()
            return [old]
        return [fresh]

    interactor.repository.get_messages = AsyncMock(side_effect=fetch)
    task = asyncio.create_task(interactor.get_chat_messages(42))
    await started.wait()
    await interactor.handle_event(
        SystemEvent(
            type="reaction_update",
            text="",
            chat_name="",
            chat_id=42,
            message_model=fresh,
        )
    )
    finish.set()
    assert (await task)[0].reactions[0].emoji == "👍"
    assert interactor.repository.get_messages.await_count == 2


async def test_concurrent_navigation_shares_the_prefetch(interactor):
    started, finish = asyncio.Event(), asyncio.Event()

    async def fetch(*args, **kwargs):
        started.set()
        await finish.wait()
        return []

    interactor.repository.get_messages = AsyncMock(side_effect=fetch)
    background = asyncio.create_task(interactor.get_chat_messages(42))
    await started.wait()
    foreground = asyncio.create_task(interactor.get_chat_messages(42))
    finish.set()
    assert await asyncio.gather(background, foreground) == [[], []]
    interactor.repository.get_messages.assert_awaited_once()


async def test_authors_query_failure_is_not_reported_as_an_empty_list():
    from src.web import create_app

    client = MagicMock()
    client.get_entity = AsyncMock(side_effect=OSError("synthetic offline"))
    ops = ChatQueryOps(client, None, None)
    app = create_app()
    app.tg_adapter = SimpleNamespace(is_connected=lambda: True, _retired=False)
    app.chat_interactor = SimpleNamespace(get_recent_authors=ops.get_recent_authors)

    response = await app.test_client().get("/api/chat/42/authors")
    assert response.status_code == 503
    assert "error" in await response.get_json()


async def test_busy_chat_does_not_retry_forever(interactor):
    async def fetch(*args, **kwargs):
        await interactor.handle_event(
            SystemEvent(type="message", text="new", chat_name="A", chat_id=42)
        )
        return []

    interactor.repository.get_messages = AsyncMock(side_effect=fetch)
    with pytest.raises(TelegramUnavailable):
        await asyncio.wait_for(interactor.get_chat_messages(42), timeout=1)
    assert interactor.repository.get_messages.await_count == 3
    assert (42, None) not in interactor._messages


async def test_message_cache_does_not_reuse_old_account_after_switch(interactor):
    from datetime import datetime

    started, finish = asyncio.Event(), asyncio.Event()
    old = Message(
        id=1, text="old", date=datetime.now(), sender_name="A", is_outgoing=False
    )
    new = Message(
        id=2, text="new", date=datetime.now(), sender_name="A", is_outgoing=False
    )

    async def fetch_old(*args, **kwargs):
        started.set()
        await finish.wait()
        return [old]

    interactor.repository.get_messages = AsyncMock(side_effect=fetch_old)
    task = asyncio.create_task(interactor.get_chat_messages(42))
    await started.wait()
    new_repo = MagicMock()
    new_repo.get_messages = AsyncMock(return_value=[new])
    interactor.replace_repository(new_repo)
    finish.set()
    assert (await task)[0].id == 2
    assert (await interactor.get_chat_messages(42))[0].id == 2
    new_repo.get_messages.assert_awaited_once()


async def test_card_does_not_return_or_cache_an_old_account_after_switch(interactor):
    started, finish = asyncio.Event(), asyncio.Event()
    old = Chat(id=42, name="Old account", unread_count=1, type=ChatType.USER)
    new = Chat(id=42, name="New account", unread_count=2, type=ChatType.USER)

    async def fetch_old(*args, **kwargs):
        started.set()
        await finish.wait()
        return old

    interactor.repository.get_chat = AsyncMock(side_effect=fetch_old)
    task = asyncio.create_task(interactor.get_chat(42, refresh=True))
    await started.wait()
    new_repo = MagicMock()
    new_repo.get_chat = AsyncMock(return_value=new)
    interactor.replace_repository(new_repo)
    finish.set()
    assert await task is new
    new_repo.get_chat.assert_awaited_once_with(42)


async def test_old_account_history_page_is_not_returned_after_switch(interactor):
    from datetime import datetime

    started, finish = asyncio.Event(), asyncio.Event()
    old = Message(
        id=1,
        text="old account",
        date=datetime.now(),
        sender_name="A",
        is_outgoing=False,
    )

    async def fetch_old(*args, **kwargs):
        started.set()
        await finish.wait()
        return [old]

    interactor.repository.get_messages = AsyncMock(side_effect=fetch_old)
    task = asyncio.create_task(interactor.get_chat_messages(42, offset_id=2))
    await started.wait()
    interactor.replace_repository(MagicMock())
    finish.set()
    with pytest.raises(AccountChanged):
        await task


async def test_list_stays_available_during_background_refresh(interactor):
    await interactor.get_chat_page()
    interactor._warmer_running = True
    started, finish = asyncio.Event(), asyncio.Event()

    async def refresh():
        started.set()
        await finish.wait()
        return make_chats(2)

    interactor.repository.get_chats.side_effect = refresh
    task = asyncio.create_task(interactor._get_chat_list(refresh=True))
    await started.wait()
    page = await asyncio.wait_for(interactor.get_chat_page(), timeout=0.5)
    assert len(page.chats) == 20
    finish.set()
    await task
    assert len((await interactor.get_chat_page()).chats) == 2


async def test_forum_topics_are_warmed_and_invalidated_by_events(interactor):
    topics = [Chat(id=11, name="Topic", unread_count=0, type=ChatType.TOPIC)]
    interactor.repository.get_forum_topics_page = AsyncMock(
        return_value=ForumTopicPage(topics, None)
    )
    assert await interactor.get_forum_topics(42) == topics
    assert await interactor.get_forum_topics(42) == topics
    interactor.repository.get_forum_topics_page.assert_awaited_once()
    await interactor.handle_event(
        SystemEvent(type="message", text="new", chat_name="Forum", chat_id=42)
    )
    assert await interactor.get_forum_topics(42) == topics
    assert interactor.repository.get_forum_topics_page.await_count == 2


async def test_chat_route_renders_telegram_posts_with_autoescaping():
    from datetime import datetime, timezone
    from src.domain.models import Message
    from src.rules.models import Rule, RuleType
    from src.users.models import User

    app = create_app()
    app.tg_adapter = SimpleNamespace(is_connected=lambda: True)
    app.user_repo = SimpleNamespace(
        get_user=AsyncMock(return_value=User(username="@me"))
    )
    app.rule_service = SimpleNamespace(
        is_autoread_enabled=AsyncMock(return_value=True),
        is_ai_autoread_enabled=AsyncMock(return_value=False),
        get_rule=AsyncMock(
            side_effect=lambda chat_id, topic_id, rule_type: (
                Rule(chat_id=chat_id) if rule_type == RuleType.AUTOREAD else None
            )
        ),
    )
    app.chat_interactor = SimpleNamespace(
        get_chat=AsyncMock(
            return_value=Chat(
                id=42, name="<Channel>", unread_count=0, type=ChatType.CHANNEL
            )
        ),
        get_chat_messages=AsyncMock(
            return_value=[
                Message(
                    id=7,
                    text="<blockquote>Quoted text</blockquote>",
                    date=datetime.now(timezone.utc),
                    sender_name="<Sender>",
                    is_outgoing=False,
                )
            ]
        ),
        get_recent_events=AsyncMock(),
    )
    response = await app.test_client().get("/chat/42")
    html = await response.get_data(as_text=True)
    assert response.status_code == 200
    assert "&lt;Channel&gt;" in html and "&lt;Sender&gt;" in html
    assert "<blockquote>Quoted text</blockquote>" in html
    assert 'aria-pressed="true"' in html
    assert '<dialog id="lightbox" aria-label="Photo viewer"' in html
    assert '<dialog id="autoreact-modal" class="modal-overlay"' in html
    assert '<details class="chat-actions-container" id="chat-controls" open>' in html
    assert '<button type="button" class="emoji-opt' in html
    assert '<button type="button" class="user-selector-toggle"' in html
    app.chat_interactor.get_recent_events.assert_not_awaited()


async def test_chat_list_reuses_and_refreshes_cache(interactor, monkeypatch):
    clock = [0]
    monkeypatch.setattr(interactors.time, "monotonic", lambda: clock[0])
    first, second = await asyncio.gather(
        interactor.get_chat_page(), interactor.get_chat_page()
    )
    assert first.snapshot != second.snapshot
    interactor.repository.get_chats.assert_awaited_once()
    await interactor.get_chat_page(refresh=True)
    assert interactor.repository.get_chats.await_count == 2
    clock[0] += interactors.CHAT_LIST_TTL
    await interactor.get_chat_page()
    assert interactor.repository.get_chats.await_count == 3


async def test_simultaneous_refreshes_share_one_dialog_scan(interactor):
    await interactor.get_chat_page()
    interactor.repository.get_chats.reset_mock()
    started, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def fetch():
        started.set()
        await release.wait()
        return make_chats(2)

    async def second():
        second_started.set()
        return await interactor.get_chat_page(refresh=True)

    interactor.repository.get_chats.side_effect = fetch
    first = asyncio.create_task(interactor.get_chat_page(refresh=True))
    await asyncio.wait_for(started.wait(), timeout=1)
    other = asyncio.create_task(second())
    await asyncio.wait_for(second_started.wait(), timeout=1)
    release.set()
    pages = await asyncio.gather(first, other)

    assert [len(page.chats) for page in pages] == [2, 2]
    interactor.repository.get_chats.assert_awaited_once()


async def test_account_switch_during_fetch_does_not_cache_previous_account(interactor):
    started, finish = asyncio.Event(), asyncio.Event()

    async def old_chats():
        started.set()
        await finish.wait()
        return make_chats(1)

    interactor.repository.get_chats = AsyncMock(side_effect=old_chats)
    task = asyncio.create_task(interactor.get_chat_page())
    await started.wait()
    new_repo = MagicMock()
    new_repo.get_chats = AsyncMock(return_value=make_chats(2))
    interactor.replace_repository(new_repo)
    finish.set()
    page = await task
    assert len(page.chats) == 2
    new_repo.get_chats.assert_awaited_once()


@pytest.mark.parametrize(
    "query", ["offset=-1", "offset=20", "offset=abc", "offset=9999999999"]
)
async def test_page_route_rejects_invalid_offsets(client, query):
    assert (await client.get(f"/api/chats?{query}")).status_code == 400


async def test_page_route_reports_expired_snapshot(client):
    response = await client.get("/api/chats?snapshot=unknown&offset=20")
    assert response.status_code == 410
