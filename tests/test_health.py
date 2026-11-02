from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.web.routes import register_routes
from src.users.models import User
from src.web.sse import EventBroadcaster


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql):
        return self

    def fetchone(self):
        return (1,)


@pytest.fixture
def app():
    _app = Quart(__name__)
    _app.sse = EventBroadcaster()
    register_routes(_app)

    client = MagicMock()
    client.is_user_authorized = AsyncMock(return_value=True)
    client.get_me = AsyncMock(return_value=object())

    _app.tg_adapter = MagicMock()
    _app.tg_adapter.client = client
    _app.tg_adapter.is_connected.return_value = True
    _app.tg_adapter.connect = AsyncMock()
    _app.tg_adapter._self_id = 123
    _app.tg_adapter._write_queue.queue_size.return_value = 0
    _app.tg_adapter._write_queue.is_running.return_value = True
    client.reconnect_count = 0

    _app.user_repo = MagicMock()
    _app.user_repo._connect.return_value = _Conn()
    _app.user_repo.get_user = AsyncMock(
        return_value=User(session_string="session", api_id=1, api_hash="hash")
    )

    _app.action_repo = MagicMock()
    _app.action_repo.redis.ping = AsyncMock(return_value=True)

    _app.event_bus = SimpleNamespace(_subscribers=[])

    return _app


@pytest.fixture
def client(app):
    return app.test_client()


async def test_api_health_returns_ok(app, client):
    response = await client.get("/api/health")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["status"] == "ok"
    assert data["db"] is True
    assert data["cache"] is True
    assert data["telegram_client_exists"] is True
    assert data["telegram_connected"] is True
    assert data["telegram_authorized"] is True
    assert data["telegram_authenticated_call"] is None
    assert data["write_queue_running"] is True
    assert data["telegram_reconnect_count"] == 0


async def test_api_health_returns_503_when_telegram_is_disconnected(app, client):
    app.tg_adapter.is_connected.return_value = False

    response = await client.get("/api/health")

    assert response.status_code == 503
    data = await response.get_json()
    assert data["status"] == "unhealthy"
    assert data["telegram_connected"] is False
    app.tg_adapter.connect.assert_not_awaited()
    app.tg_adapter.client.get_me.assert_not_awaited()
    app.tg_adapter.client.is_user_authorized.assert_not_awaited()


async def test_liveness_is_available_without_an_account(app, client):
    app.user_repo.get_user.return_value = None
    app.tg_adapter.is_connected.return_value = False
    live = await client.get("/api/live")
    ready = await client.get("/api/health")
    assert live.status_code == 200
    assert ready.status_code == 503
    assert (await ready.get_json())["account_configured"] is False
    app.tg_adapter.connect.assert_not_awaited()


async def test_api_health_returns_503_when_db_fails(app, client):
    app.user_repo._connect.side_effect = RuntimeError("db down")

    response = await client.get("/api/health")

    assert response.status_code == 503
    data = await response.get_json()
    assert data["status"] == "unhealthy"
    assert data["db"] is False


async def test_api_health_returns_503_when_cache_fails(app, client):
    app.action_repo.redis.ping.side_effect = RuntimeError("cache down")

    response = await client.get("/api/health")

    assert response.status_code == 503
    data = await response.get_json()
    assert data["status"] == "unhealthy"
    assert data["cache"] is False


async def test_health_alias_is_not_registered(client):
    response = await client.get("/health")

    assert response.status_code == 404


async def test_api_health_is_unhealthy_if_write_worker_has_stopped(app, client):
    app.tg_adapter._write_queue.is_running.return_value = False
    response = await client.get("/api/health")
    assert response.status_code == 503
    assert (await response.get_json())["write_queue_running"] is False
