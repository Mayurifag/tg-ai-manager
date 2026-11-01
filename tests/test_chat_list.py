import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart
from telethon import types

from src.adapters.telegram.chat_query_ops import ChatQueryOps
from src.application import interactors
from src.application.interactors import ChatInteractor, ChatSnapshotExpired
from src.domain.models import Chat, ChatType, Message, SystemEvent
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


async def test_busy_chat_does_not_retry_forever(interactor):
    async def fetch(*args, **kwargs):
        await interactor.handle_event(
            SystemEvent(type="message", text="new", chat_name="A", chat_id=42)
        )
        return []

    interactor.repository.get_messages = AsyncMock(side_effect=fetch)
    assert await asyncio.wait_for(interactor.get_chat_messages(42), timeout=1) == []
    assert interactor.repository.get_messages.await_count == 3


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
    interactor.repository.get_forum_topics = AsyncMock(return_value=topics)
    assert await interactor.get_forum_topics(42) == topics
    assert await interactor.get_forum_topics(42) == topics
    interactor.repository.get_forum_topics.assert_awaited_once()
    await interactor.handle_event(
        SystemEvent(type="message", text="new", chat_name="Forum", chat_id=42)
    )
    assert await interactor.get_forum_topics(42) == topics
    assert interactor.repository.get_forum_topics.await_count == 2


async def test_chat_route_renders_telegram_posts_with_autoescaping():
    from datetime import datetime, timezone
    from src.domain.models import Message
    from src.users.models import User

    app = create_app()
    app.tg_adapter = SimpleNamespace(is_connected=lambda: True)
    app.user_repo = SimpleNamespace(
        get_user=AsyncMock(return_value=User(username="@me"))
    )
    app.rule_service = SimpleNamespace(
        is_autoread_enabled=AsyncMock(return_value=True),
        is_ai_autoread_enabled=AsyncMock(return_value=False),
        get_rule=AsyncMock(return_value=None),
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
