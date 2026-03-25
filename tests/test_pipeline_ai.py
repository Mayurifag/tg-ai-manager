"""Tests for AI classification integration in handle_new_message_event (S03)."""

from unittest.mock import AsyncMock, MagicMock, patch

from src.domain.models import SystemEvent
from src.rules.models import RuleType
from src.users.models import User
from tests.test_rules import make_message, make_rule, make_service


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def make_event(
    chat_id: int = 100,
    text: str = "Buy now! Great deal!",
    chat_name: str = "Test Chat",
) -> SystemEvent:
    msg = make_message(text=text)
    return SystemEvent(
        type="message",
        text=text,
        chat_name=chat_name,
        chat_id=chat_id,
        message_model=msg,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_ai_skipped_when_already_should_read():
    """If an AUTOREAD rule already fired, GeminiClassifier is never instantiated."""
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(chat_id=100, rule_type=RuleType.AUTOREAD),
        make_rule(chat_id=100, rule_type=RuleType.AI_AUTOREAD),
    ]
    user_repo = AsyncMock()
    user_repo.get_user.return_value = User(
        ai_api_key="test-key", ai_model="gemini-2.0-flash"
    )
    svc = make_service(rule_repo=rule_repo, user_repo=user_repo)

    with patch("src.rules.service.GeminiClassifier") as mock_cls:
        await svc.handle_new_message_event(make_event())
        mock_cls.assert_not_called()


async def test_ai_skipped_when_ai_autoread_not_enabled():
    """If there is no AI_AUTOREAD rule, GeminiClassifier is never instantiated."""
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = []  # No rules at all
    user_repo = AsyncMock()
    user_repo.get_user.return_value = User(
        ai_api_key="test-key", ai_model="gemini-2.0-flash"
    )
    svc = make_service(rule_repo=rule_repo, user_repo=user_repo)

    with patch("src.rules.service.GeminiClassifier") as mock_cls:
        await svc.handle_new_message_event(make_event())
        mock_cls.assert_not_called()


async def test_ai_skipped_when_no_api_key():
    """AI_AUTOREAD is enabled but user has no api_key — classifier never instantiated."""
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(chat_id=100, rule_type=RuleType.AI_AUTOREAD),
    ]
    user_repo = AsyncMock()
    user_repo.get_user.return_value = User(ai_api_key=None, ai_model="gemini-2.0-flash")
    svc = make_service(rule_repo=rule_repo, user_repo=user_repo)

    with patch("src.rules.service.GeminiClassifier") as mock_cls:
        await svc.handle_new_message_event(make_event())
        mock_cls.assert_not_called()


async def test_ai_classifies_ad_marks_read():
    """When classifier returns True (ad), message is marked read with ai_ad_detected."""
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(chat_id=100, rule_type=RuleType.AI_AUTOREAD),
    ]
    user_repo = AsyncMock()
    user_repo.get_user.return_value = User(
        ai_api_key="test-key", ai_model="gemini-2.0-flash"
    )
    action_repo = AsyncMock()
    chat_repo = AsyncMock()
    svc = make_service(
        rule_repo=rule_repo,
        user_repo=user_repo,
        action_repo=action_repo,
        chat_repo=chat_repo,
    )

    mock_instance = MagicMock()
    mock_instance.classify_is_ad = AsyncMock(return_value=True)

    with patch("src.rules.service.GeminiClassifier", return_value=mock_instance):
        await svc.handle_new_message_event(make_event())

    chat_repo.mark_as_read.assert_called_once()
    action_repo.add_log.assert_called_once()
    log_call = action_repo.add_log.call_args[0][0]
    assert log_call.reason == "ai_ad_detected"


async def test_ai_classifies_not_ad_skips():
    """When classifier returns False (not ad), message is NOT marked read."""
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(chat_id=100, rule_type=RuleType.AI_AUTOREAD),
    ]
    user_repo = AsyncMock()
    user_repo.get_user.return_value = User(
        ai_api_key="test-key", ai_model="gemini-2.0-flash"
    )
    action_repo = AsyncMock()
    chat_repo = AsyncMock()
    svc = make_service(
        rule_repo=rule_repo,
        user_repo=user_repo,
        action_repo=action_repo,
        chat_repo=chat_repo,
    )

    mock_instance = MagicMock()
    mock_instance.classify_is_ad = AsyncMock(return_value=False)

    with patch("src.rules.service.GeminiClassifier", return_value=mock_instance):
        await svc.handle_new_message_event(make_event())

    chat_repo.mark_as_read.assert_not_called()
    action_repo.add_log.assert_not_called()


async def test_ai_failure_logs_warning_skips():
    """On classifier exception, message stays unread (no false positive)."""
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(chat_id=100, rule_type=RuleType.AI_AUTOREAD),
    ]
    user_repo = AsyncMock()
    user_repo.get_user.return_value = User(
        ai_api_key="test-key", ai_model="gemini-2.0-flash"
    )
    action_repo = AsyncMock()
    chat_repo = AsyncMock()
    svc = make_service(
        rule_repo=rule_repo,
        user_repo=user_repo,
        action_repo=action_repo,
        chat_repo=chat_repo,
    )

    mock_instance = MagicMock()
    mock_instance.classify_is_ad = AsyncMock(side_effect=Exception("API error"))

    with patch("src.rules.service.GeminiClassifier", return_value=mock_instance):
        # Should not raise
        await svc.handle_new_message_event(make_event())

    chat_repo.mark_as_read.assert_not_called()
    action_repo.add_log.assert_not_called()


async def test_startup_scan_no_ai():
    """run_startup_scan never calls handle_new_message_event, so AI is never invoked."""
    from src.domain.models import Chat, ChatType

    chat = Chat(id=100, name="Test", unread_count=3, type=ChatType.GROUP)
    rule_repo = AsyncMock()
    # AI_AUTOREAD rule present but no AUTOREAD rule
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(chat_id=100, rule_type=RuleType.AI_AUTOREAD),
    ]
    user_repo = AsyncMock()
    user_repo.get_user.return_value = User(
        ai_api_key="test-key", ai_model="gemini-2.0-flash"
    )
    action_repo = AsyncMock()
    chat_repo = AsyncMock()
    chat_repo.is_connected = MagicMock(return_value=True)
    chat_repo.get_all_unread_chats.return_value = [chat]
    svc = make_service(
        rule_repo=rule_repo,
        user_repo=user_repo,
        action_repo=action_repo,
        chat_repo=chat_repo,
    )

    with patch("src.rules.service.GeminiClassifier") as mock_cls:
        await svc.run_startup_scan()
        mock_cls.assert_not_called()

    # No AUTOREAD rule, so chat should not have been marked read
    chat_repo.mark_as_read.assert_not_called()
