"""Tests for AI settings backend: PATCH /api/settings AI fields + POST /api/rules/ai_autoread/toggle."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.users.models import User
from src.web.routes import register_routes
from src.web import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Minimal Quart app with routes registered and mock services attached."""
    _app = Quart(__name__)
    _app.login_lock = asyncio.Lock()
    register_routes(_app)

    _app.user_repo = AsyncMock()
    _app.rule_service = AsyncMock()
    _app.tg_adapter = MagicMock()
    _app.tg_adapter.is_connected.return_value = True  # bypass login_required middleware

    return _app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_settings_dialogs_and_toggles_have_accessible_controls():
    app = create_app()
    app.tg_adapter = SimpleNamespace(is_connected=lambda: True)
    app.user_repo = SimpleNamespace(get_user=AsyncMock(return_value=User()))
    app.chat_interactor = SimpleNamespace(get_recent_events=AsyncMock())

    response = await app.test_client().get("/settings")
    html = await response.get_data(as_text=True)

    assert response.status_code == 200
    assert html.count('<dialog id="modal-') == 5
    assert html.count('class="modal-close"') == 5
    assert html.count('class="modal-status" role="status"') == 5
    assert "onchange=\"toggleSetting('autoread_polls')\"" in html
    assert 'onclick="toggleSetting(' not in html
    assert 'id="settings-status" role="status"' in html


async def test_patch_settings_with_ai_fields(app, client):
    """PATCH /api/settings with AI fields → save_user called with those values."""
    current_user = User(id=1)
    app.user_repo.get_user.return_value = current_user
    app.user_repo.update_fields.return_value = True

    response = await client.patch(
        "/api/settings",
        headers={"X-Requested-With": "XMLHttpRequest"},
        json={
            "ai_provider": "gemini",
            "ai_model": "gemini-test",
            "ai_api_key": "sk-test-key",
            "ai_prompt": "You are helpful.",
        },
    )

    assert response.status_code == 200
    data = await response.get_json()
    assert data["status"] == "ok"

    app.user_repo.update_fields.assert_awaited_once_with(
        1,
        {
            "ai_provider": "gemini",
            "ai_model": "gemini-test",
            "ai_api_key": "sk-test-key",
            "ai_prompt": "You are helpful.",
        },
        expected_session=None,
    )


async def test_patch_settings_preserves_ai_fields_when_absent(app, client):
    """PATCH /api/settings with unrelated field only → AI fields retain current values."""
    current_user = User(
        id=1,
        ai_provider="anthropic",
        ai_model="claude-3-opus",
        ai_api_key="existing-key",
        ai_prompt="Existing prompt.",
    )
    app.user_repo.get_user.return_value = current_user
    app.user_repo.update_fields.return_value = True

    response = await client.patch(
        "/api/settings",
        headers={"X-Requested-With": "XMLHttpRequest"},
        json={"debug_mode": True},
    )

    assert response.status_code == 200

    app.user_repo.update_fields.assert_awaited_once_with(
        1, {"debug_mode": True}, expected_session=None
    )


async def test_toggle_ai_autoread_enable(app, client):
    """POST /api/rules/ai_autoread/toggle with enabled=true → toggle_ai_autoread called."""
    app.rule_service.toggle_ai_autoread.return_value = None

    response = await client.post(
        "/api/rules/ai_autoread/toggle",
        headers={"X-Requested-With": "XMLHttpRequest"},
        json={"chat_id": 100, "enabled": True},
    )

    assert response.status_code == 200
    data = await response.get_json()
    assert data["status"] == "ok"

    app.rule_service.toggle_ai_autoread.assert_called_once_with(100, None, True)


async def test_toggle_ai_autoread_disable(app, client):
    """POST /api/rules/ai_autoread/toggle with enabled=false → disable path called."""
    app.rule_service.toggle_ai_autoread.return_value = None

    response = await client.post(
        "/api/rules/ai_autoread/toggle",
        headers={"X-Requested-With": "XMLHttpRequest"},
        json={"chat_id": 100, "enabled": False},
    )

    assert response.status_code == 200
    data = await response.get_json()
    assert data["status"] == "ok"

    app.rule_service.toggle_ai_autoread.assert_called_once_with(100, None, False)


async def test_toggle_ai_autoread_missing_chat_id(app, client):
    """POST /api/rules/ai_autoread/toggle with no chat_id → 400 response."""
    response = await client.post(
        "/api/rules/ai_autoread/toggle",
        headers={"X-Requested-With": "XMLHttpRequest"},
        json={},
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "chat_id" in data.get("error", "")

    app.rule_service.toggle_ai_autoread.assert_not_called()
