import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telethon import types

from src.adapters.telegram.chat_query_ops import ChatQueryOps
from src.domain.models import Chat, ChatType
from src.infrastructure.maintenance import job_telegram_recovery
from src.rules.models import RuleType
from tests.test_rules import make_rule, make_service


def make_reconciliation_service(rules, chats):
    rule_repo = AsyncMock()
    rule_repo.get_all.return_value = rules
    rule_repo.get_by_chat_and_topic.side_effect = lambda cid, tid=None: [
        r for r in rules if r.chat_id == cid and r.topic_id in (None, tid)
    ]
    chat_repo = MagicMock()
    chat_repo.is_connected.return_value = True
    chat_repo.get_all_unread_chats = AsyncMock(return_value=chats)
    chat_repo.get_unread_topics = AsyncMock(return_value=[])
    chat_repo.mark_as_read = AsyncMock()
    return make_service(rule_repo=rule_repo, chat_repo=chat_repo)


async def test_reconciliation_only_queries_explicit_autoread_rules():
    rules = [
        make_rule(100, RuleType.AUTOREAD),
        make_rule(200, RuleType.AUTOREACT),
        make_rule(300, RuleType.AI_AUTOREAD),
    ]
    chat = Chat(100, "Allowed", 2, ChatType.GROUP, last_message_id=99)
    svc = make_reconciliation_service(rules, [chat])
    await svc.reconcile_autoread()
    svc.chat_repo.get_all_unread_chats.assert_awaited_once_with([100])
    svc.chat_repo.mark_as_read.assert_awaited_once_with(100, max_id=99)


async def test_reconciliation_does_not_clear_sibling_topics():
    rules = [make_rule(100, RuleType.AUTOREAD, topic_id=5)]
    svc = make_reconciliation_service(rules, [Chat(100, "Forum", 3, ChatType.FORUM)])
    svc.chat_repo.get_unread_topics.return_value = [
        Chat(5, "Allowed", 1, ChatType.TOPIC, last_message_id=50),
        Chat(6, "Not allowed", 2, ChatType.TOPIC, last_message_id=60),
    ]
    await svc.reconcile_autoread()
    svc.chat_repo.mark_as_read.assert_awaited_once_with(100, 5, max_id=50)


async def test_reconciliation_rechecks_rules_disabled_during_query():
    svc = make_reconciliation_service(
        [make_rule(100, RuleType.AUTOREAD)],
        [Chat(100, "Chat", 1, ChatType.GROUP, last_message_id=99)],
    )
    svc.rule_repo.get_by_chat_and_topic.side_effect = None
    svc.rule_repo.get_by_chat_and_topic.return_value = []
    await svc.reconcile_autoread()
    svc.chat_repo.mark_as_read.assert_not_awaited()


async def test_reconciliation_without_rules_does_not_query_telegram():
    svc = make_reconciliation_service([], [])
    await svc.reconcile_autoread()
    svc.chat_repo.get_all_unread_chats.assert_not_awaited()


async def test_batch_unread_query_uses_server_state_not_history_or_avatars():
    entity = types.Chat(
        id=100,
        title="Allowed",
        photo=types.ChatPhotoEmpty(),
        participants_count=1,
        date=datetime.now(timezone.utc),
        version=1,
    )
    client = AsyncMock()
    client.get_input_entity.return_value = types.InputPeerChat(100)
    client.return_value = SimpleNamespace(
        chats=[entity],
        users=[],
        dialogs=[
            SimpleNamespace(
                peer=types.PeerChat(100),
                unread_count=2,
                top_message=99,
                read_inbox_max_id=97,
            ),
        ],
    )
    media = MagicMock()
    ops = ChatQueryOps(client, None, media)
    chats = await ops.get_all_unread_chats([-100, -100])
    assert len(chats) == 1
    assert chats[0].unread_count == 2
    assert chats[0].last_message_id == 99
    client.get_input_entity.assert_awaited_once_with(-100)
    client.assert_awaited_once()
    client.get_messages.assert_not_awaited()
    media._get_chat_image.assert_not_called()


async def test_recovery_reconnects_then_reconciles_and_stops_cleanly():
    shutdown = asyncio.Event()
    svc = MagicMock()
    svc.chat_repo.is_connected.side_effect = [False, True]
    svc.chat_repo.connect = AsyncMock()
    svc.reconcile_autoread = AsyncMock(side_effect=shutdown.set)
    user_repo = AsyncMock()
    user_repo.get_user.return_value = SimpleNamespace(is_authenticated=lambda: True)
    with patch("src.infrastructure.maintenance.TELEGRAM_RECOVERY_INTERVAL", 0.001):
        await asyncio.wait_for(job_telegram_recovery(svc, user_repo, shutdown), 1)
    svc.chat_repo.connect.assert_awaited_once()
    svc.reconcile_autoread.assert_awaited_once()


async def test_inaccessible_rule_does_not_repeat_failed_lookup_every_scan():
    client = AsyncMock()
    client.get_input_entity.side_effect = ValueError("Chat not found")
    ops = ChatQueryOps(client, None, None)
    assert await ops.get_all_unread_chats([-100]) == []
    assert await ops.get_all_unread_chats([-100]) == []
    client.get_input_entity.assert_awaited_once_with(-100)
    client.assert_not_awaited()
    ops._unavailable_chats[-100] = 0
    assert await ops.get_all_unread_chats([-100]) == []
    assert client.get_input_entity.await_count == 2
