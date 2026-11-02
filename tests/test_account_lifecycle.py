import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from quart import Quart

from src.container import bind_adapter_events, reload_tg_adapter
from src.infrastructure.event_bus import EventBus
from src.rules.decisions import ReadDecision
from src.web.routes import register_routes
from tests.test_rules import make_message, make_service
from src.domain.models import SystemEvent
from src.users.models import User


async def test_delayed_decision_cannot_write_using_a_replacement_adapter():
    old_adapter = AsyncMock()
    new_adapter = AsyncMock()
    service = make_service(chat_repo=old_adapter)
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_decision(event, message, snapshot):
        started.set()
        await release.wait()
        return ReadDecision(True, "ai_ad_detected", max_id=message.id)

    service.get_read_decision = delayed_decision
    event = SystemEvent(
        type="message",
        text="ad",
        chat_name="chat",
        chat_id=1,
        message_model=make_message(),
    )
    task = asyncio.create_task(service.handle_new_message_event(event))
    await asyncio.wait_for(started.wait(), 1)
    service.chat_repo = new_adapter
    release.set()
    await asyncio.wait_for(task, 1)
    old_adapter.mark_as_read.assert_not_awaited()
    new_adapter.mark_as_read.assert_not_awaited()


async def test_retired_adapter_events_cannot_reach_new_account():
    app = Quart(__name__)
    app.event_bus = EventBus()
    old, new = MagicMock(), MagicMock()
    old._retired = new._retired = False
    app.tg_adapter = old
    app.rule_service = MagicMock()
    app.rule_service.retire_account = AsyncMock()
    app.chat_interactor = MagicMock()
    app.sse = MagicMock()
    delivery_started, finish_delivery = asyncio.Event(), asyncio.Event()
    disconnect_started, finish_disconnect = asyncio.Event(), asyncio.Event()
    delivered = []

    async def pause_old_event(event):
        if event.text == "old in-flight":
            delivery_started.set()
            await finish_delivery.wait()

    async def automate(event):
        delivered.append((event.text, app.tg_adapter))

    async def disconnect():
        disconnect_started.set()
        await finish_disconnect.wait()

    old.disconnect = AsyncMock(side_effect=disconnect)
    app.event_bus.subscribe(pause_old_event)
    app.event_bus.subscribe(automate)
    bind_adapter_events(app, old)
    old_callback = old.add_event_listener.call_args.args[0]

    async with app.app_context():
        with (
            patch("src.container.TelethonAdapter", return_value=new),
            patch(
                "src.container.get_settings",
                return_value=SimpleNamespace(TG_API_ID=1, TG_API_HASH="synthetic"),
            ),
        ):
            first = asyncio.create_task(
                old_callback(
                    SystemEvent(type="message", text="old in-flight", chat_name="")
                )
            )
            await asyncio.wait_for(delivery_started.wait(), 1)
            replacement = asyncio.create_task(reload_tg_adapter())
            await asyncio.wait_for(disconnect_started.wait(), 1)
            assert old._retired
            app.sse.invalidate_clients.assert_called_once()
            # The old adapter remains current until disconnect finishes, but
            # events arriving in this interval must already be rejected.
            await old_callback(
                SystemEvent(type="message", text="old late", chat_name="")
            )
            assert delivered == []
            finish_disconnect.set()
            await asyncio.wait_for(replacement, 1)
            finish_delivery.set()
            await asyncio.wait_for(first, 1)
            assert delivered == []
            new_callback = new.add_event_listener.call_args.args[0]
            await new_callback(SystemEvent(type="message", text="new", chat_name=""))

    assert delivered == [("new", new)]


async def test_reset_replaces_adapter_before_forgetting_the_user():
    app = Quart(__name__)
    register_routes(app)
    app.login_lock = asyncio.Lock()
    app.tg_adapter = MagicMock()
    app.tg_adapter.is_connected.return_value = True
    app.user_repo = AsyncMock()
    steps = []

    async def reload(*args, **kwargs):
        steps.append("replace")

    async def delete(*args):
        steps.append("delete")

    app.user_repo.delete_user.side_effect = delete
    with patch("src.web.routes.settings.reload_tg_adapter", side_effect=reload):
        response = await app.test_client().post(
            "/api/settings/reset", headers={"X-Requested-With": "XMLHttpRequest"}
        )
    assert response.status_code == 200
    assert steps == ["replace", "delete"]


async def test_disconnected_account_can_be_explicitly_forgotten():
    app = Quart(__name__)
    register_routes(app)
    app.login_lock = asyncio.Lock()
    app.tg_adapter = MagicMock()
    app.tg_adapter.is_connected.return_value = False
    app.user_repo = AsyncMock()
    with patch(
        "src.web.routes.settings.reload_tg_adapter", new_callable=AsyncMock
    ) as reload:
        response = await app.test_client().post(
            "/api/settings/reset", headers={"X-Requested-With": "XMLHttpRequest"}
        )
    assert response.status_code == 200
    reload.assert_awaited_once()
    app.user_repo.delete_user.assert_awaited_once_with(1)


def login_app(saved_account_id, new_account_id):
    app = Quart(__name__)
    register_routes(app)
    app.login_lock = asyncio.Lock()
    app.tg_adapter = MagicMock()
    app.tg_adapter._login_attempt_id = "current-attempt"
    app.tg_adapter._expected_login_account_id = saved_account_id
    app.tg_adapter._login_finalized = False
    app.tg_adapter.get_qr_status.return_value = "authorized"
    app.tg_adapter.get_session_string.return_value = "new-session"
    app.tg_adapter.client.get_me = AsyncMock(
        return_value=SimpleNamespace(
            id=new_account_id, username="synthetic", premium=True
        )
    )
    app.tg_adapter.activate_login = AsyncMock()
    app.tg_adapter.sign_in_with_password = AsyncMock()
    app.user_repo = AsyncMock()
    app.user_repo.get_user.return_value = User(
        session_string="old-session", debug_mode=True
    )
    app.user_repo.update_fields.return_value = True
    app.rule_service = MagicMock()
    app.rule_service.run_startup_scan = AsyncMock()
    app.managed_tasks = MagicMock()

    def close_scheduled(coro, name):
        coro.close()

    app.managed_tasks.create.side_effect = close_scheduled
    return app


async def test_overlapping_status_requests_finalize_same_account_once():
    app = login_app(42, 42)
    settings = SimpleNamespace(TG_API_ID=1, TG_API_HASH="hash", RULES_SYNC_URL=None)
    with patch("src.web.routes.auth.get_settings", return_value=settings):
        first, second = await asyncio.gather(
            app.test_client().get("/api/auth/qr/status?attempt_id=current-attempt"),
            app.test_client().get("/api/auth/qr/status?attempt_id=current-attempt"),
        )
    assert first.status_code == second.status_code == 200
    app.user_repo.update_fields.assert_awaited_once()
    assert (
        app.user_repo.update_fields.await_args.kwargs["expected_session"]
        == "old-session"
    )
    app.tg_adapter.activate_login.assert_awaited_once_with("new-session")
    app.managed_tasks.create.assert_called_once()


async def test_different_account_never_reuses_saved_rules_or_activates():
    app = login_app(42, 99)
    settings = SimpleNamespace(TG_API_ID=1, TG_API_HASH="hash", RULES_SYNC_URL=None)
    with patch("src.web.routes.auth.get_settings", return_value=settings):
        response = await app.test_client().get(
            "/api/auth/qr/status?attempt_id=current-attempt"
        )
    assert response.status_code == 409
    app.user_repo.update_fields.assert_not_awaited()
    app.tg_adapter.activate_login.assert_not_awaited()
    app.managed_tasks.create.assert_not_called()


async def test_stale_attempt_cannot_poll_or_submit_password():
    app = login_app(42, 42)
    client = app.test_client()
    response = await client.get("/api/auth/qr/status?attempt_id=stale-attempt")
    assert response.status_code == 409
    response = await client.post(
        "/api/auth/2fa",
        json={"attempt_id": "stale-attempt", "password": "secret"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert response.status_code == 409
    app.tg_adapter.sign_in_with_password.assert_not_awaited()
    app.user_repo.update_fields.assert_not_awaited()
