"""Tests for RuleService: autoread, autoreact, and global rule matching."""

from datetime import datetime
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.models import Chat, ChatType, Message, Reaction, SystemEvent
from src.rules.models import Rule, RuleType
from src.rules.service import RuleService
from src.users.models import User


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------


def make_rule(
    chat_id: int,
    rule_type: RuleType,
    topic_id: Optional[int] = None,
    config: Optional[dict] = None,
) -> Rule:
    return Rule(
        id=1,
        user_id=1,
        rule_type=rule_type,
        chat_id=chat_id,
        topic_id=topic_id,
        config=config or {},
    )


def make_message(
    id: int = 1,
    text: str = "hello",
    sender_id: int = 999,
    is_outgoing: bool = False,
    is_service: bool = False,
    is_poll: bool = False,
    sender_username: Optional[str] = None,
    reactions: Optional[List[Reaction]] = None,
    grouped_id: Optional[int] = None,
) -> Message:
    return Message(
        id=id,
        text=text,
        date=datetime.now(),
        sender_name="Sender",
        is_outgoing=is_outgoing,
        sender_id=sender_id,
        sender_username=sender_username,
        is_service=is_service,
        is_poll=is_poll,
        reactions=reactions or [],
        grouped_id=grouped_id,
    )


def make_service(
    rule_repo=None,
    action_repo=None,
    chat_repo=None,
    user_repo=None,
    user: Optional[User] = None,
) -> RuleService:
    if rule_repo is None:
        rule_repo = AsyncMock()
        rule_repo.get_by_chat_and_topic.return_value = []
    if action_repo is None:
        action_repo = AsyncMock()
    if chat_repo is None:
        chat_repo = AsyncMock()
    if user_repo is None:
        user_repo = AsyncMock()
        user_repo.get_user.return_value = user or User()
    return RuleService(rule_repo, action_repo, chat_repo, user_repo)


# ---------------------------------------------------------------------------
# is_autoread_enabled
# ---------------------------------------------------------------------------


async def test_is_autoread_enabled_returns_true_when_rule_exists():
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(chat_id=100, rule_type=RuleType.AUTOREAD)
    ]
    svc = make_service(rule_repo=rule_repo)
    assert await svc.is_autoread_enabled(100) is True


async def test_is_autoread_enabled_returns_false_when_no_rule():
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = []
    svc = make_service(rule_repo=rule_repo)
    assert await svc.is_autoread_enabled(100) is False


async def test_is_autoread_enabled_topic_specific_takes_priority():
    """Topic-specific rule overrides chat-level absence."""
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(chat_id=100, rule_type=RuleType.AUTOREAD, topic_id=5)
    ]
    svc = make_service(rule_repo=rule_repo)
    assert await svc.is_autoread_enabled(100, topic_id=5) is True


# ---------------------------------------------------------------------------
# check_global_autoread_rules
# ---------------------------------------------------------------------------


async def test_global_autoread_skips_when_unread_count_gt_1():
    user = User(autoread_service_messages=True)
    svc = make_service(user=user)
    msg = make_message(is_service=True)
    result = await svc.check_global_autoread_rules(msg, unread_count=2)
    assert result == ""


async def test_global_autoread_matches_service_message():
    user = User(autoread_service_messages=True)
    svc = make_service(user=user)
    msg = make_message(is_service=True)
    result = await svc.check_global_autoread_rules(msg, unread_count=1)
    assert result == "global_service_msg"


async def test_global_autoread_matches_poll():
    user = User(autoread_polls=True)
    svc = make_service(user=user)
    msg = make_message(is_poll=True)
    result = await svc.check_global_autoread_rules(msg, unread_count=1)
    assert result == "global_poll"


async def test_global_autoread_matches_self():
    user = User(autoread_self=True)
    svc = make_service(user=user)
    msg = make_message(is_outgoing=True)
    result = await svc.check_global_autoread_rules(msg, unread_count=1)
    assert result == "global_self"


async def test_global_autoread_matches_bot_by_username():
    user = User(autoread_bots="@mybot,@otherbot")
    svc = make_service(user=user)
    msg = make_message(sender_username="mybot")
    result = await svc.check_global_autoread_rules(msg, unread_count=1)
    assert result == "global_bot_mybot"


async def test_global_autoread_no_match_for_nonbot():
    user = User(autoread_bots="@mybot")
    svc = make_service(user=user)
    msg = make_message(sender_username="someone_else")
    result = await svc.check_global_autoread_rules(msg, unread_count=1)
    assert result == ""


async def test_global_autoread_matches_regex():
    user = User(autoread_regex=r"^ping$")
    svc = make_service(user=user)
    msg = make_message(text="ping")
    result = await svc.check_global_autoread_rules(msg, unread_count=1)
    assert result == "global_regex"


async def test_global_autoread_regex_no_match():
    user = User(autoread_regex=r"^ping$")
    svc = make_service(user=user)
    msg = make_message(text="pong")
    result = await svc.check_global_autoread_rules(msg, unread_count=1)
    assert result == ""


# ---------------------------------------------------------------------------
# apply_autoreact
# ---------------------------------------------------------------------------


async def test_apply_autoreact_skips_outgoing():
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(100, RuleType.AUTOREACT, config={"emoji": "👍", "target_users": []})
    ]
    chat_repo = AsyncMock()
    svc = make_service(rule_repo=rule_repo, chat_repo=chat_repo)
    msg = make_message(is_outgoing=True)
    await svc.apply_autoreact(100, None, msg)
    chat_repo.send_reaction.assert_not_called()


async def test_apply_autoreact_fires_for_matching_sender():
    sender_id = 42
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(
            100,
            RuleType.AUTOREACT,
            config={"emoji": "💩", "target_users": [sender_id]},
        )
    ]
    chat_repo = AsyncMock()
    svc = make_service(rule_repo=rule_repo, chat_repo=chat_repo)
    msg = make_message(sender_id=sender_id)
    await svc.apply_autoreact(100, None, msg)
    chat_repo.send_reaction.assert_called_once_with(100, msg.id, "💩")


async def test_apply_autoreact_skips_nonmatching_sender():
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(
            100,
            RuleType.AUTOREACT,
            config={"emoji": "💩", "target_users": [999]},
        )
    ]
    chat_repo = AsyncMock()
    svc = make_service(rule_repo=rule_repo, chat_repo=chat_repo)
    msg = make_message(sender_id=1)  # different sender
    await svc.apply_autoreact(100, None, msg)
    chat_repo.send_reaction.assert_not_called()


async def test_apply_autoreact_fires_for_all_senders_when_no_targets():
    """Empty target_users list means react to everyone."""
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(100, RuleType.AUTOREACT, config={"emoji": "❤️", "target_users": []})
    ]
    chat_repo = AsyncMock()
    svc = make_service(rule_repo=rule_repo, chat_repo=chat_repo)
    msg = make_message(sender_id=12345)
    await svc.apply_autoreact(100, None, msg)
    chat_repo.send_reaction.assert_called_once_with(100, msg.id, "❤️")


async def test_apply_autoreact_skips_already_reacted():
    """Does not send reaction if the emoji is already chosen."""
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(100, RuleType.AUTOREACT, config={"emoji": "👍", "target_users": []})
    ]
    chat_repo = AsyncMock()
    svc = make_service(rule_repo=rule_repo, chat_repo=chat_repo)
    reaction = Reaction(emoji="👍", count=1, is_chosen=True)
    msg = make_message(reactions=[reaction])
    await svc.apply_autoreact(100, None, msg)
    chat_repo.send_reaction.assert_not_called()


async def test_apply_autoreact_album_dedup():
    """Only one reaction is sent per album group_id."""
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(100, RuleType.AUTOREACT, config={"emoji": "🔥", "target_users": []})
    ]
    chat_repo = AsyncMock()
    svc = make_service(rule_repo=rule_repo, chat_repo=chat_repo)

    msg1 = make_message(id=1, grouped_id=777)
    msg2 = make_message(id=2, grouped_id=777)

    await svc.apply_autoreact(100, None, msg1)
    await svc.apply_autoreact(100, None, msg2)

    # Should only react once for the album
    chat_repo.send_reaction.assert_called_once()


# ---------------------------------------------------------------------------
# _toggle_rule
# ---------------------------------------------------------------------------


async def test_toggle_rule_creates_new_rule_when_enabled():
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = []
    rule_repo.add.return_value = 1
    svc = make_service(rule_repo=rule_repo)
    result = await svc.toggle_autoread(100, None, enabled=True)
    rule_repo.add.assert_called_once()
    assert result is not None


async def test_toggle_rule_deletes_existing_when_disabled():
    existing = make_rule(100, RuleType.AUTOREAD)
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [existing]
    svc = make_service(rule_repo=rule_repo)
    result = await svc.toggle_autoread(100, None, enabled=False)
    rule_repo.delete.assert_called_once_with(existing.id)
    assert result is None
