"""Tests for AI settings backend: PATCH /api/settings AI fields + POST /api/rules/ai_autoread/toggle."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.users.models import User
from src.web.routes import register_routes


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Minimal Quart app with routes registered and mock services attached."""
    _app = Quart(__name__)
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


async def test_patch_settings_with_ai_fields(app, client):
    """PATCH /api/settings with AI fields → save_user called with those values."""
    current_user = User(id=1)
    app.user_repo.get_user.return_value = current_user
    app.user_repo.save_user.return_value = None

    response = await client.patch(
        "/api/settings",
        json={
            "ai_provider": "openai",
            "ai_model": "gpt-4o",
            "ai_api_key": "sk-test-key",
            "ai_prompt": "You are helpful.",
        },
    )

    assert response.status_code == 200
    data = await response.get_json()
    assert data["status"] == "ok"

    app.user_repo.save_user.assert_called_once()
    saved_user = app.user_repo.save_user.call_args[0][0]
    assert saved_user.ai_provider == "openai"
    assert saved_user.ai_model == "gpt-4o"
    assert saved_user.ai_api_key == "sk-test-key"
    assert saved_user.ai_prompt == "You are helpful."


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
    app.user_repo.save_user.return_value = None

    response = await client.patch(
        "/api/settings",
        json={"debug_mode": True},
    )

    assert response.status_code == 200

    app.user_repo.save_user.assert_called_once()
    saved_user = app.user_repo.save_user.call_args[0][0]
    # AI fields must be preserved from current_user
    assert saved_user.ai_provider == "anthropic"
    assert saved_user.ai_model == "claude-3-opus"
    assert saved_user.ai_api_key == "existing-key"
    assert saved_user.ai_prompt == "Existing prompt."
    # The changed field
    assert saved_user.debug_mode is True


async def test_toggle_ai_autoread_enable(app, client):
    """POST /api/rules/ai_autoread/toggle with enabled=true → toggle_ai_autoread called."""
    app.rule_service.toggle_ai_autoread.return_value = None

    response = await client.post(
        "/api/rules/ai_autoread/toggle",
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
        json={},
    )

    assert response.status_code == 400
    data = await response.get_json()
    assert "chat_id" in data.get("error", "")

    app.rule_service.toggle_ai_autoread.assert_not_called()
