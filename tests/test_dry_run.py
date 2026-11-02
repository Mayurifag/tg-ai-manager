import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon import types
from quart import Quart

from src.adapters.telegram.chat_query_ops import ChatQueryOps
from src.adapters.telegram.message_parser import MessageParser
from src.adapters.telegram.read_ops import ReadOps
from src.domain.errors import AccountChanged
from src.domain.models import Reaction, SystemEvent
from src.requests import BadRequest, DebugProcessRequest
from src.rules.models import RuleType
from src.users.models import User
from src.web.routes import register_routes
from tests.test_read_ops import get_topic_name
from tests.test_rules import make_message, make_rule, make_service
from tests.test_selective_read_safety import service_with_state


def context(message, topic_id=None, scope_known=True):
    return SimpleNamespace(message=message, topic_id=topic_id, scope_known=scope_known)


async def test_dry_run_reports_same_selective_safety_veto_as_real_execution():
    service, telegram, queue = service_with_state([1, 2], User(autoread_regex="ad"))
    message = make_message(id=2, text="ad")
    service.chat_repo.get_message_context.return_value = context(message)
    reads = ReadOps(telegram, queue, None, get_topic_name)
    service.chat_repo.can_selectively_read.side_effect = reads.can_selectively_read

    result = await service.simulate_process_message(1, 2)
    assert result["autoread"]["status"] == "blocked"
    assert result["autoread"]["reason"] == "unread_safety_veto"
    assert result["autoread"]["matched_reason"] == "global_regex"
    assert queue.items == [] and telegram.acknowledged == []
    service.chat_repo.mark_automatic_as_read.assert_not_awaited()
    service.chat_repo.send_automatic_reaction.assert_not_awaited()

    event = SystemEvent(
        type="message", text="ad", chat_name="", chat_id=1, message_model=message
    )
    await service.handle_new_message_event(event)
    await queue.run_next()
    assert telegram.unread == [1, 2]
    assert telegram.acknowledged == []


@pytest.mark.parametrize(
    "message,reason",
    [
        (make_message(is_outgoing=True), "outgoing"),
        (make_message(is_service=True), "service_message"),
        (make_message(sender_id=9), "sender_mismatch"),
        (
            make_message(sender_id=42, reactions=[Reaction("👍", 1, True)]),
            "already_chosen",
        ),
    ],
)
async def test_dry_run_reaction_guards_match_live_planning(message, reason):
    rule = make_rule(
        100, RuleType.AUTOREACT, config={"emoji": "👍", "target_users": [42]}
    )
    service = make_service()
    service.rule_repo.get_by_chat_and_topic.return_value = [rule]
    service.chat_repo.get_message_context.return_value = context(message, topic_id=7)
    result = await service.simulate_process_message(100, message.id, topic_id=7)
    assert result["autoreact"]["reason"] == reason
    assert result["autoreact"]["inherited"] is True
    await service.apply_autoreact(100, 7, message)
    service.chat_repo.send_automatic_reaction.assert_not_awaited()


async def test_dry_run_resolves_topic_and_never_classifies_without_explicit_opt_in():
    rule = make_rule(100, RuleType.AI_AUTOREAD, topic_id=7)
    user = User(ai_api_key="synthetic", ai_model="synthetic")
    service = make_service(user=user)
    service.rule_repo.get_by_chat_and_topic.return_value = [rule]
    message = make_message(id=3, text="ad")
    service.chat_repo.get_message_context.return_value = context(message, topic_id=7)
    service.chat_repo.can_selectively_read.return_value = True
    classifier = SimpleNamespace(classify_is_ad=AsyncMock(return_value=True))
    service._ai_classifier_factory = MagicMock(return_value=classifier)

    result = await service.simulate_process_message(100, 3)
    assert result["scope"]["topic_id"] == 7
    assert result["autoread"]["status"] == "not_evaluated"
    assert result["autoread"]["reason"] == "ai_not_evaluated"
    classifier.classify_is_ad.assert_not_awaited()

    result = await service.simulate_process_message(
        100, 3, topic_id=7, evaluate_ai=True
    )
    assert result["autoread"]["status"] == "would_read"
    assert result["autoread"]["reason"] == "ai_ad_detected"
    assert result["autoread"]["safety"] == "passed_current_state"
    classifier.classify_is_ad.assert_awaited_once_with("ad")
    service.chat_repo.mark_automatic_as_read.assert_not_awaited()


async def test_global_selective_match_is_not_mislabeled_as_inherited_ai():
    service = make_service(user=User(autoread_regex="ad"))
    service.rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(100, RuleType.AI_AUTOREAD)
    ]
    service.chat_repo.get_message_context.return_value = context(
        make_message(id=3, text="ad"), topic_id=7
    )
    service.chat_repo.can_selectively_read.return_value = True
    result = await service.simulate_process_message(100, 3)
    assert result["autoread"]["reason"] == "global_regex"
    assert result["autoread"]["inherited"] is False


async def test_dry_run_blanket_and_album_state_match_live_planning():
    service = make_service()
    service.rule_repo.get_by_chat_and_topic.return_value = [
        make_rule(100, RuleType.AUTOREAD),
        make_rule(100, RuleType.AUTOREACT, config={"emoji": "👍"}),
    ]
    message = make_message(id=3, grouped_id=123)
    service.chat_repo.get_message_context.return_value = context(message, topic_id=7)
    first = await service.simulate_process_message(100, 3)
    assert first["autoread"]["status"] == "would_read"
    assert first["autoread"]["inherited"] is True
    assert first["autoreact"]["status"] == "would_react"
    service.chat_repo.can_selectively_read.assert_not_awaited()
    await service.apply_autoreact(100, 7, message)
    second = await service.simulate_process_message(100, 3)
    assert second["autoreact"]["reason"] == "album_already_pending_or_reacted"
    assert service.chat_repo.send_automatic_reaction.await_count == 1


async def test_dry_run_rejects_wrong_or_unknown_topic_without_planning():
    service = make_service()
    service.chat_repo.get_message_context.return_value = context(
        make_message(), topic_id=8
    )
    result = await service.simulate_process_message(100, 1, topic_id=7)
    assert result["error"] == "Message does not belong to this topic"
    service.rule_repo.get_by_chat_and_topic.assert_not_awaited()
    service.chat_repo.get_message_context.return_value = context(
        make_message(), scope_known=False
    )
    result = await service.simulate_process_message(100, 1)
    assert result["error"] == "Message topic is unknown"
    service.rule_repo.get_by_chat_and_topic.assert_not_awaited()


async def test_dry_run_route_validates_ai_opt_in_and_passes_topic_scope():
    service = make_service()
    service.chat_repo.is_connected = MagicMock(return_value=True)
    service.chat_repo.get_message_context.return_value = context(
        make_message(), topic_id=7
    )
    app = Quart(__name__)
    app.tg_adapter = service.chat_repo
    app.rule_service = service
    app.login_lock = asyncio.Lock()
    register_routes(app)
    client = app.test_client()
    headers = {"X-Requested-With": "XMLHttpRequest"}

    response = await client.post(
        "/api/debug/process",
        json={"chat_id": 100, "msg_id": 1, "topic_id": 7},
        headers=headers,
    )
    assert response.status_code == 200
    assert (await response.get_json())["scope"]["topic_id"] == 7
    response = await client.post(
        "/api/debug/process",
        json={"chat_id": 100, "msg_id": 1, "evaluate_ai": "true"},
        headers=headers,
    )
    assert response.status_code == 400
    response = await client.post(
        "/api/debug/process",
        json={"chat_id": 100, "msg_id": 1, "topic_id": 8},
        headers=headers,
    )
    assert response.status_code == 400
    assert (await response.get_json())[
        "error"
    ] == "Message does not belong to this topic"
    service.chat_repo.mark_automatic_as_read.assert_not_awaited()
    service.chat_repo.send_automatic_reaction.assert_not_awaited()


async def test_dry_run_aborts_if_account_changes_during_message_fetch():
    service = make_service()
    started, release = asyncio.Event(), asyncio.Event()

    async def fetch(*_):
        started.set()
        await release.wait()
        return context(make_message())

    service.chat_repo.get_message_context.side_effect = fetch
    task = asyncio.create_task(service.simulate_process_message(100, 1))
    await asyncio.wait_for(started.wait(), 1)
    await service.retire_account()
    service.chat_repo = AsyncMock()
    release.set()
    with pytest.raises(AccountChanged):
        await task
    service.rule_repo.get_by_chat_and_topic.assert_not_awaited()


@pytest.mark.parametrize("value", ["true", 1, None])
def test_ai_opt_in_requires_a_boolean(value):
    with pytest.raises(BadRequest):
        DebugProcessRequest.from_json({"chat_id": 1, "msg_id": 2, "evaluate_ai": value})


async def test_message_context_uses_real_forum_topic_header():
    client = AsyncMock()
    client.get_entity.return_value = SimpleNamespace(forum=True)
    raw = SimpleNamespace(
        id=42, reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_top_id=7)
    )
    client.get_messages.return_value = [raw]
    parser = MessageParser(client, MagicMock())
    parser._parse_message = AsyncMock(return_value=make_message(id=42))
    ops = ChatQueryOps(client, parser, MagicMock())
    result = await ops.get_message_context(100, 42)
    assert result.topic_id == 7 and result.scope_known
    raw.reply_to = types.MessageReplyHeader(forum_topic=True)
    result = await ops.get_message_context(100, 42)
    assert result.topic_id is None and not result.scope_known
    raw.reply_to = None
    result = await ops.get_message_context(100, 42)
    assert result.topic_id == 1 and result.scope_known
