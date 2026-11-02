"""Tests for RuleService: autoread, autoreact, and global rule matching."""

from datetime import datetime
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

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


async def test_regex_uses_raw_telegram_text_not_presentation_markup():
    svc = make_service(user=User(autoread_regex=r"^hello <world>$"))
    msg = make_message(text="<strong>hello</strong> &lt;world&gt;")
    msg.raw_text = "hello <world>"
    assert await svc.check_global_autoread_rules(msg, unread_count=1) == "global_regex"


async def test_disabled_event_uses_one_rules_and_settings_snapshot():
    svc = make_service(user=User(autoread_bots=""))
    event = SystemEvent(
        type="message",
        text="hi",
        chat_name="chat",
        chat_id=100,
        message_model=make_message(),
    )
    await svc.handle_new_message_event(event)
    svc.rule_repo.get_by_chat_and_topic.assert_awaited_once_with(100, None)
    svc.user_repo.get_user.assert_awaited_once_with(1)


async def test_pathological_regex_times_out_without_blocking_event_loop():
    import asyncio

    svc = make_service(user=User(autoread_regex=r"(a+)+$"))
    msg = make_message(text="a" * 8000 + "!")
    progress = asyncio.Event()

    async def heartbeat():
        await asyncio.sleep(0.005)
        progress.set()

    task = asyncio.create_task(heartbeat())
    result = await asyncio.wait_for(
        svc.check_global_autoread_rules(msg, unread_count=1), 1
    )
    assert progress.is_set()
    await task
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
    chat_repo.send_automatic_reaction.assert_not_called()


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
    args = chat_repo.send_automatic_reaction.await_args.args
    assert args[:3] == (100, msg.id, "💩")
    assert await args[3]()


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
    chat_repo.send_automatic_reaction.assert_not_called()


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
    assert chat_repo.send_automatic_reaction.await_args.args[:3] == (100, msg.id, "❤️")


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
    chat_repo.send_automatic_reaction.assert_not_called()


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
    chat_repo.send_automatic_reaction.assert_awaited_once()


async def test_ineligible_album_part_does_not_suppress_eligible_part():
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(100, RuleType.AUTOREACT, config={"emoji": "👍", "target_users": [42]})
    ]
    svc = make_service(rule_repo=rule_repo)
    await svc.apply_autoreact(100, None, make_message(id=1, grouped_id=7, sender_id=99))
    await svc.apply_autoreact(100, None, make_message(id=2, grouped_id=7, sender_id=42))
    assert svc.chat_repo.send_automatic_reaction.await_args.args[:3] == (100, 2, "👍")


async def test_failed_album_reaction_allows_another_part_to_retry():
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(100, RuleType.AUTOREACT, config={"emoji": "👍"})
    ]
    svc = make_service(rule_repo=rule_repo)
    await svc.apply_autoreact(100, None, make_message(id=1, grouped_id=7))
    await svc.chat_repo.send_automatic_reaction.await_args.args[4](False)
    await svc.apply_autoreact(100, None, make_message(id=2, grouped_id=7))
    assert svc.chat_repo.send_automatic_reaction.await_count == 2


# ---------------------------------------------------------------------------
# _toggle_rule
# ---------------------------------------------------------------------------


async def test_toggle_rule_creates_new_rule_when_enabled():
    rule_repo = AsyncMock()
    rule_repo.set_scope.return_value = make_rule(100, RuleType.AUTOREAD)
    svc = make_service(rule_repo=rule_repo)
    result = await svc.toggle_autoread(100, None, enabled=True)
    rule_repo.set_scope.assert_awaited_once()
    assert result is not None


async def test_toggle_rule_deletes_existing_when_disabled():
    rule_repo = AsyncMock()
    rule_repo.set_scope.return_value = None
    svc = make_service(rule_repo=rule_repo)
    result = await svc.toggle_autoread(100, None, enabled=False)
    rule_repo.set_scope.assert_awaited_once()
    assert result is None


# ---------------------------------------------------------------------------
# run_startup_scan
# ---------------------------------------------------------------------------


def make_chat(
    id: int = 1,
    name: str = "Chat",
    unread_count: int = 3,
    chat_type: ChatType = ChatType.GROUP,
) -> Chat:
    return Chat(id=id, name=name, unread_count=unread_count, type=chat_type)


async def test_startup_scan_skips_chat_with_no_autoread_rule():
    """Non-forum chat with multiple unread and no rule is skipped (not read)."""
    chat = make_chat(id=1, unread_count=5)
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = []
    action_repo = AsyncMock()
    chat_repo = AsyncMock()
    chat_repo.is_connected = MagicMock(return_value=True)
    chat_repo.get_all_unread_chats.return_value = [chat]
    svc = make_service(
        rule_repo=rule_repo, action_repo=action_repo, chat_repo=chat_repo
    )

    await svc.run_startup_scan()

    chat_repo.mark_as_read.assert_not_called()
    action_repo.add_log.assert_not_called()


async def test_startup_scan_marks_read_chat_with_autoread_rule():
    """Non-forum chat with an autoread rule is marked read and logged."""
    chat = make_chat(id=42, unread_count=7)
    chat.last_message_id = 70
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(chat_id=42, rule_type=RuleType.AUTOREAD)
    ]
    action_repo = AsyncMock()
    chat_repo = AsyncMock()
    chat_repo.is_connected = MagicMock(return_value=True)
    chat_repo.get_all_unread_chats.return_value = [chat]
    svc = make_service(
        rule_repo=rule_repo, action_repo=action_repo, chat_repo=chat_repo
    )

    await svc.run_startup_scan()

    args, kwargs = chat_repo.mark_automatic_as_read.await_args
    assert args[:3] == (42, None, 70)
    assert kwargs == {"selective": False}
    assert await args[3]()
    action_repo.add_log.assert_not_awaited()
    await args[4]()
    action_repo.add_log.assert_called_once()
    log_call = action_repo.add_log.call_args[0][0]
    assert log_call.action == "startup_read"
    assert log_call.chat_id == 42
    assert log_call.reason == "autoread_rule_startup"


async def test_startup_scan_forum_chat_checks_topics():
    """Forum chat iterates topics; only topics with autoread rules are read."""
    forum_chat = make_chat(id=100, unread_count=2, chat_type=ChatType.FORUM)
    topic_with_rule = make_chat(
        id=10, name="Topic 10", unread_count=1, chat_type=ChatType.TOPIC
    )
    topic_no_rule = make_chat(
        id=20, name="Topic 20", unread_count=1, chat_type=ChatType.TOPIC
    )
    topic_with_rule.last_message_id = 50
    topic_rule = make_rule(chat_id=100, rule_type=RuleType.AUTOREAD, topic_id=10)

    def rule_side_effect(chat_id, topic_id):
        if chat_id == 100 and topic_id == 10:
            return [topic_rule]
        return []

    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.side_effect = rule_side_effect
    action_repo = AsyncMock()
    chat_repo = AsyncMock()
    chat_repo.is_connected = MagicMock(return_value=True)
    chat_repo.get_all_unread_chats.return_value = [forum_chat]
    chat_repo.get_unread_topics.return_value = [topic_with_rule, topic_no_rule]
    svc = make_service(
        rule_repo=rule_repo, action_repo=action_repo, chat_repo=chat_repo
    )

    await svc.run_startup_scan()

    # Only topic 10 (with rule) should be marked read
    args, kwargs = chat_repo.mark_automatic_as_read.await_args
    assert args[:3] == (100, 10, 50)
    assert kwargs == {"selective": False}
    assert await args[3]()
    await args[4]()
    action_repo.add_log.assert_called_once()
    log_call = action_repo.add_log.call_args[0][0]
    assert log_call.action == "startup_read"
    assert "Topic 10" in log_call.chat_name


# ---------------------------------------------------------------------------
# handle_new_message_event — action type (e.g. pin service messages)
# ---------------------------------------------------------------------------


async def test_handle_new_message_event_autoread_on_action():
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(chat_id=100, rule_type=RuleType.AUTOREAD)
    ]
    action_repo = AsyncMock()
    chat_repo = AsyncMock()
    svc = make_service(
        rule_repo=rule_repo, action_repo=action_repo, chat_repo=chat_repo
    )

    msg = make_message(id=42, is_service=True)
    event = SystemEvent(
        type="action",
        text="Admin pinned a message",
        chat_name="TestChat",
        chat_id=100,
        message_model=msg,
    )
    await svc.handle_new_message_event(event)

    args, kwargs = chat_repo.mark_automatic_as_read.await_args
    assert args[:3] == (100, None, 42)
    assert kwargs == {"selective": False}
    assert event.is_read is False
    await args[4]()
    assert event.is_read is True


async def test_handle_new_message_event_topic_autoread_uses_message_id():
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(chat_id=100, rule_type=RuleType.AUTOREAD)
    ]
    action_repo = AsyncMock()
    chat_repo = AsyncMock()
    svc = make_service(
        rule_repo=rule_repo, action_repo=action_repo, chat_repo=chat_repo
    )

    msg = make_message(id=42)
    event = SystemEvent(
        type="message",
        text="hello",
        chat_name="TestChat",
        chat_id=100,
        topic_id=7,
        message_model=msg,
    )
    await svc.handle_new_message_event(event)

    args, kwargs = chat_repo.mark_automatic_as_read.await_args
    assert args[:3] == (100, 7, 42)
    assert kwargs == {"selective": False}
    await args[4]()
    assert event.is_read is True


async def test_handle_new_message_event_queues_guarded_selective_read():
    svc = make_service(user=User(autoread_service_messages=True))

    msg = make_message(id=42, is_service=True)
    event = SystemEvent(
        type="action",
        text="Admin pinned a message",
        chat_name="TestChat",
        chat_id=100,
        message_model=msg,
    )
    await svc.handle_new_message_event(event)

    args, kwargs = svc.chat_repo.mark_automatic_as_read.await_args
    assert args[:3] == (100, None, 42)
    assert kwargs == {"selective": True}
    assert event.is_read is False
    await args[4]()
    assert event.is_read is True


async def test_apply_autoreact_skips_service_message():
    """Service messages are never reacted to even with a blanket autoreact rule."""
    rule_repo = AsyncMock()
    rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(100, RuleType.AUTOREACT, config={"emoji": "👍", "target_users": []})
    ]
    chat_repo = AsyncMock()
    svc = make_service(rule_repo=rule_repo, chat_repo=chat_repo)
    msg = make_message(is_service=True)
    await svc.apply_autoreact(100, None, msg)
    chat_repo.send_automatic_reaction.assert_not_called()
