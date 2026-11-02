import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart
from telethon import errors

from src.adapters.telegram.chat_query_ops import ChatQueryOps
from src.adapters.valkey_repo import ValkeyActionRepository, ValkeyEventRepository
from src.application.interactors import ChatInteractor
from src.domain.errors import LogUnavailable
from src.domain.models import Chat, ChatType
from src.users.models import User
from src.web import create_app
from src.web.routes import register_routes


@pytest.fixture
def app():
    app = Quart(__name__)
    register_routes(app)
    app.login_lock = asyncio.Lock()
    app.tg_adapter = MagicMock()
    app.tg_adapter.is_connected.return_value = True
    app.user_repo = AsyncMock()
    app.rule_service = AsyncMock()
    return app


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://evil.example"},
        {"X-Requested-With": "XMLHttpRequest", "Origin": "https://evil.example"},
        {"X-Requested-With": "XMLHttpRequest", "Origin": "null"},
        {
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://localhost.evil.example",
        },
        {"X-Requested-With": "XMLHttpRequest", "Origin": "https://localhost:444"},
        {"X-Requested-With": "XMLHttpRequest", "Sec-Fetch-Site": "cross-site"},
    ],
)
async def test_reset_rejects_cross_origin_or_simple_requests(app, headers):
    response = await app.test_client().post("/api/settings/reset", headers=headers)
    assert response.status_code == 403
    app.user_repo.delete_user.assert_not_awaited()


async def test_same_origin_mutation_passes_csrf_guard(app):
    app.rule_service.toggle_autoread.return_value = None
    response = await app.test_client().post(
        "/api/rules/autoread/toggle",
        headers={"X-Requested-With": "XMLHttpRequest", "Origin": "http://localhost"},
        json={"chat_id": 123, "enabled": False},
    )
    assert response.status_code == 200
    app.rule_service.toggle_autoread.assert_awaited_once()


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/rules/autoread/toggle", {"chat_id": 123, "enabled": True}),
        ("/api/settings", {"debug_mode": True}),
        ("/api/chat/123/read", {}),
    ],
)
async def test_mutation_waiting_on_old_account_cannot_act_on_replacement(
    app, path, body
):
    checked = asyncio.Event()
    old = app.tg_adapter

    def is_connected():
        checked.set()
        return True

    old.is_connected.side_effect = is_connected
    replacement = MagicMock()
    replacement.is_connected.return_value = True
    app.chat_interactor = AsyncMock()
    await app.login_lock.acquire()
    try:
        task = asyncio.create_task(
            app.test_client().post(
                path, json=body, headers={"X-Requested-With": "XMLHttpRequest"}
            )
        )
        await asyncio.wait_for(checked.wait(), 1)
        app.tg_adapter = replacement
    finally:
        app.login_lock.release()
    response = await asyncio.wait_for(task, 1)
    assert response.status_code == 409
    app.rule_service.toggle_autoread.assert_not_awaited()
    app.user_repo.update_fields.assert_not_awaited()
    app.chat_interactor.mark_chat_as_read.assert_not_awaited()


async def test_mutation_lock_is_released_when_a_handler_fails(app):
    app.user_repo.get_user.side_effect = RuntimeError("synthetic database failure")
    response = await app.test_client().post(
        "/api/settings",
        json={"debug_mode": True},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert response.status_code == 500
    assert not app.login_lock.locked()


async def test_account_reset_waits_for_inflight_rule_change(app, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def change_rule(*_):
        started.set()
        await release.wait()

    app.rule_service.toggle_autoread.side_effect = change_rule
    reload_adapter = AsyncMock()
    monkeypatch.setattr("src.web.routes.settings.reload_tg_adapter", reload_adapter)
    client = app.test_client()
    mutation = asyncio.create_task(
        client.post(
            "/api/rules/autoread/toggle",
            json={"chat_id": 123, "enabled": True},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    reset = asyncio.create_task(
        client.post(
            "/api/settings/reset", headers={"X-Requested-With": "XMLHttpRequest"}
        )
    )
    await asyncio.sleep(0.01)
    reload_adapter.assert_not_awaited()
    release.set()
    assert (await asyncio.wait_for(mutation, 1)).status_code == 200
    assert (await asyncio.wait_for(reset, 1)).status_code == 200
    reload_adapter.assert_awaited_once()
    assert not app.login_lock.locked()


async def test_response_from_old_account_is_not_returned_after_replacement(app):
    started, release = asyncio.Event(), asyncio.Event()

    async def fetch_chat(*_):
        started.set()
        await release.wait()
        return Chat(
            id=123, name="Old account private chat", unread_count=0, type=ChatType.USER
        )

    app.chat_interactor = MagicMock()
    app.chat_interactor.get_chat = AsyncMock(side_effect=fetch_chat)
    request_task = asyncio.create_task(app.test_client().get("/api/chat/123/info"))
    await asyncio.wait_for(started.wait(), 1)
    app.tg_adapter = MagicMock()
    app.tg_adapter.is_connected.return_value = True
    release.set()
    response = await asyncio.wait_for(request_task, 1)
    assert response.status_code == 409
    assert "Old account" not in (await response.get_data(as_text=True))


async def test_first_unread_endpoint_checks_topic_and_only_reads_state(app):
    app.chat_interactor = AsyncMock()
    app.chat_interactor.first_unread_message_id.return_value = 8
    client = app.test_client()
    response = await client.get("/api/chat/-100/first-unread?topic_id=7")
    assert response.status_code == 200
    assert await response.get_json() == {"message_id": 8}
    app.chat_interactor.first_unread_message_id.assert_awaited_once_with(-100, 7)
    app.chat_interactor.mark_chat_as_read.assert_not_awaited()
    response = await client.get("/api/chat/-100/first-unread?topic_id=-1")
    assert response.status_code == 400
    assert app.chat_interactor.first_unread_message_id.await_count == 1


async def test_discarded_old_account_stream_releases_its_lease(app):
    started, release = asyncio.Event(), asyncio.Event()
    closed = False

    async def get_document(*_):
        started.set()
        await release.wait()
        return SimpleNamespace(size=4, mime_type="application/pdf", attributes=[])

    class Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            return b"data"

        async def aclose(self):
            nonlocal closed
            closed = True

    app.tg_adapter.get_stream_document = AsyncMock(side_effect=get_document)
    app.tg_adapter.stream_document.return_value = Stream()
    task = asyncio.create_task(app.test_client().get("/media/stream/123/1"))
    await asyncio.wait_for(started.wait(), 1)
    app.tg_adapter = MagicMock()
    app.tg_adapter.is_connected.return_value = True
    release.set()
    response = await asyncio.wait_for(task, 1)
    assert response.status_code == 409
    assert closed


async def test_preflight_does_not_grant_cross_origin_permission(app):
    response = await app.test_client().options(
        "/api/settings/reset",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-requested-with",
        },
    )
    assert "Access-Control-Allow-Origin" not in response.headers
    assert "Access-Control-Allow-Headers" not in response.headers
    app.user_repo.delete_user.assert_not_awaited()


async def test_action_log_outage_is_not_displayed_as_empty_history():
    app = create_app()
    app.tg_adapter = SimpleNamespace(is_connected=lambda: True, _retired=False)
    app.user_repo = SimpleNamespace(get_user=AsyncMock(return_value=User()))
    actions = ValkeyActionRepository("redis://localhost")
    actions.redis = AsyncMock()
    actions.redis.zrevrange.side_effect = OSError("synthetic cache outage")
    app.chat_interactor = SimpleNamespace(get_action_logs=actions.get_logs)

    response = await app.test_client().get("/actions")
    html = await response.get_data(as_text=True)
    assert response.status_code == 503
    assert "Action history is unavailable" in html
    assert "No actions recorded yet" not in html


async def test_debug_event_outage_does_not_break_the_action_page():
    app = create_app()
    app.tg_adapter = SimpleNamespace(is_connected=lambda: True, _retired=False)
    app.user_repo = SimpleNamespace(
        get_user=AsyncMock(return_value=User(debug_mode=True))
    )
    events = ValkeyEventRepository("redis://localhost")
    events.redis = AsyncMock()
    events.redis.zrevrange.side_effect = OSError("synthetic cache outage")
    app.chat_interactor = SimpleNamespace(
        get_action_logs=AsyncMock(return_value=[]),
        get_recent_events=events.get_recent_events,
    )

    response = await app.test_client().get("/actions")
    html = await response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Event history is unavailable" in html
    assert "No recent events" not in html


async def test_zero_log_limit_never_reads_the_whole_sorted_set():
    actions = ValkeyActionRepository("redis://localhost")
    actions.redis = AsyncMock()
    assert await actions.get_logs(limit=0) == []
    actions.redis.zrevrange.assert_not_awaited()


async def test_malformed_stored_event_is_reported_as_unavailable():
    events = ValkeyEventRepository("redis://localhost")
    events.redis = AsyncMock()
    events.redis.zrevrange.return_value = ['{"date": "not-a-date"}']
    with pytest.raises(LogUnavailable):
        await events.get_recent_events()


async def test_log_fetch_has_a_bounded_maximum():
    actions = ValkeyActionRepository("redis://localhost")
    actions.redis = AsyncMock()
    actions.redis.zrevrange.return_value = []
    assert await actions.get_logs(limit=1_000_000) == []
    actions.redis.zrevrange.assert_awaited_once_with("action_log", 0, 999)


async def test_disconnected_api_is_json_and_private_headers_are_set(app):
    app.tg_adapter.is_connected.return_value = False
    response = await app.test_client().get("/api/rules")
    assert response.status_code == 503
    assert (await response.get_json())["error"] == "Telegram account is not connected"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


async def test_rate_limited_history_is_retryable_and_does_not_claim_empty(
    app, monkeypatch
):
    telegram = MagicMock()
    telegram.get_input_entity = AsyncMock(return_value="peer")
    telegram.get_messages = AsyncMock(
        side_effect=[errors.FloodWaitError(request=None, capture=12), []]
    )
    ops = ChatQueryOps(telegram, MagicMock(), MagicMock())
    app.chat_interactor = ChatInteractor(ops, MagicMock(), MagicMock())
    monkeypatch.setattr(
        "src.web.routes.chat.render_template", AsyncMock(return_value="")
    )
    client = app.test_client()

    response = await client.get("/api/chat/42/history?offset_id=100")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "12"
    assert "unavailable" in (await response.get_json())["error"]

    retried = await client.get("/api/chat/42/history?offset_id=100")
    assert retried.status_code == 200
    assert (await retried.get_json())["count"] == 0
    assert telegram.get_messages.await_count == 2
