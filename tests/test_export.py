"""Tests for GET /api/rules/export — boundary contract for S01→S02."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from quart import Quart

from src.rules.models import Rule, RuleType
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

    # Mocks read by container.py accessors via current_app
    _app.user_repo = AsyncMock()
    _app.rule_service = AsyncMock()
    _app.tg_adapter = MagicMock()
    _app.tg_adapter.is_connected.return_value = False  # simulate no TG session

    return _app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_rule(chat_id=100, topic_id=None, config=None):
    return Rule(
        id=5,
        user_id=1,
        rule_type=RuleType.AUTOREAD,
        chat_id=chat_id,
        topic_id=topic_id,
        config=config or {},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_export_returns_rules_and_user_settings(app, client):
    """Happy path: rules present, user exists — correct shape returned."""
    user = User(autoread_service_messages=True, autoread_bots="@bot1")
    rule = _make_rule()

    app.user_repo.get_user.return_value = user

    with (
        patch("src.web.routes.settings.SqliteRuleRepository") as MockRepo,
        patch("src.web.routes.settings.get_settings") as mock_settings,
    ):
        mock_settings.return_value = MagicMock(DB_PATH=":memory:")
        instance = MockRepo.return_value
        instance.get_all = AsyncMock(return_value=[rule])

        response = await client.get("/api/rules/export")

    assert response.status_code == 200
    data = await response.get_json()

    # Top-level keys must be exactly these two
    assert set(data.keys()) == {"rules", "user_settings"}

    # Rule shape
    assert len(data["rules"]) == 1
    rule_obj = data["rules"][0]
    assert set(rule_obj.keys()) == {"rule_type", "chat_id", "topic_id", "config"}
    # Sensitive / internal fields must NOT be present
    for forbidden in ("id", "user_id", "created_at", "updated_at"):
        assert forbidden not in rule_obj, f"rule must not expose {forbidden!r}"

    # rule_type is the enum value string
    assert rule_obj["rule_type"] == "autoread"
    assert rule_obj["chat_id"] == 100
    assert rule_obj["topic_id"] is None
    assert rule_obj["config"] == {}

    # User settings shape
    us = data["user_settings"]
    expected_user_keys = {
        "autoread_service_messages",
        "autoread_polls",
        "autoread_self",
        "autoread_bots",
        "autoread_regex",
        "debug_mode",
        "ai_provider",
        "ai_model",
        "ai_api_key",
        "ai_prompt",
    }
    assert set(us.keys()) == expected_user_keys
    # Sensitive fields must NOT leak
    for forbidden in ("api_id", "api_hash", "session_string", "username", "is_premium"):
        assert forbidden not in us, f"user_settings must not expose {forbidden!r}"

    assert us["autoread_service_messages"] is True
    assert us["autoread_bots"] == "@bot1"


async def test_export_empty_rules(app, client):
    """Empty rules list with a valid user — rules is [] and user_settings populated."""
    user = User()
    app.user_repo.get_user.return_value = user

    with (
        patch("src.web.routes.settings.SqliteRuleRepository") as MockRepo,
        patch("src.web.routes.settings.get_settings") as mock_settings,
    ):
        mock_settings.return_value = MagicMock(DB_PATH=":memory:")
        instance = MockRepo.return_value
        instance.get_all = AsyncMock(return_value=[])

        response = await client.get("/api/rules/export")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["rules"] == []
    assert "autoread_service_messages" in data["user_settings"]


async def test_export_no_user_returns_defaults(app, client):
    """When get_user returns None, User() defaults are used."""
    app.user_repo.get_user.return_value = None

    with (
        patch("src.web.routes.settings.SqliteRuleRepository") as MockRepo,
        patch("src.web.routes.settings.get_settings") as mock_settings,
    ):
        mock_settings.return_value = MagicMock(DB_PATH=":memory:")
        instance = MockRepo.return_value
        instance.get_all = AsyncMock(return_value=[])

        response = await client.get("/api/rules/export")

    assert response.status_code == 200
    data = await response.get_json()
    us = data["user_settings"]

    # All boolean defaults are False
    assert us["autoread_service_messages"] is False
    assert us["autoread_polls"] is False
    assert us["autoread_self"] is False
    assert us["debug_mode"] is False

    # String defaults match User dataclass
    assert us["autoread_bots"] == "@lolsBotCatcherBot"
    assert us["autoread_regex"] == ""


async def test_export_bypasses_login_required(app, client):
    """Endpoint must return 200 JSON even when tg_adapter.is_connected() is False."""
    # tg_adapter.is_connected() already returns False from fixture
    assert app.tg_adapter.is_connected() is False

    app.user_repo.get_user.return_value = User()

    with (
        patch("src.web.routes.settings.SqliteRuleRepository") as MockRepo,
        patch("src.web.routes.settings.get_settings") as mock_settings,
    ):
        mock_settings.return_value = MagicMock(DB_PATH=":memory:")
        instance = MockRepo.return_value
        instance.get_all = AsyncMock(return_value=[])

        response = await client.get("/api/rules/export")

    # Must not redirect to /login; must return JSON 200
    assert response.status_code == 200
    assert response.content_type.startswith("application/json")
