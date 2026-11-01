from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.adapters.telegram.chat_query_ops import ChatQueryOps
from src.application import interactors
from src.application.interactors import ChatInteractor, ChatSnapshotExpired
from src.domain.models import Chat, ChatType
from src.web.routes.chat import chat_bp


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
    assert html.index('data-chat-id="45"') < html.index('data-chat-id="0"')
    assert 'data-chat-id="1" data-is-unread="true"' in html
    assert 'data-is-pinned="true"' in html
    assert 'data-next-offset="20"' in html

    first = await (await client.get("/api/chats")).get_json()
    second_response = await client.get(
        f"/api/chats?snapshot={first['snapshot']}&offset={first['next_offset']}"
    )
    second = await second_response.get_json()
    assert second_response.status_code == 200
    assert second["next_offset"] == 40
    assert 'data-chat-id="18"' in second["html"]
    assert 'data-chat-id="45"' not in second["html"]


@pytest.mark.parametrize(
    "query", ["offset=-1", "offset=20", "offset=abc", "offset=9999999999"]
)
async def test_page_route_rejects_invalid_offsets(client, query):
    assert (await client.get(f"/api/chats?{query}")).status_code == 400


async def test_page_route_reports_expired_snapshot(client):
    response = await client.get("/api/chats?snapshot=unknown&offset=20")
    assert response.status_code == 410
