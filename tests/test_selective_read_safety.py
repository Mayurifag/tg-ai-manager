import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import errors, functions, types

from src.adapters.telegram.read_ops import ReadOps
from src.domain.models import SystemEvent
from src.rules.models import RuleType
from src.users.models import User
from tests.test_read_ops import ManualQueue, get_topic_name
from tests.test_rules import make_message, make_rule, make_service


class StatefulTelegram:
    def __init__(self, unread):
        self.unread = list(unread)
        self.read_through = 0
        self.acknowledged = []

    async def get_input_entity(self, chat_id):
        return types.InputPeerUser(user_id=chat_id, access_hash=1)

    async def __call__(self, request):
        assert isinstance(request, functions.messages.GetPeerDialogsRequest)
        return SimpleNamespace(
            dialogs=[
                SimpleNamespace(
                    peer=types.PeerUser(1),
                    unread_count=len(self.unread),
                    top_message=max(self.unread, default=self.read_through),
                    read_inbox_max_id=self.read_through,
                )
            ]
        )

    async def send_read_acknowledge(self, entity, max_id=None):
        self.acknowledged.append(max_id)
        self.read_through = max_id
        self.unread = [message_id for message_id in self.unread if message_id > max_id]

    async def get_entity(self, entity):
        return types.User(id=1, first_name="Test")


async def run_selective(unread, message_id, *, disable=False, new_message=None):
    telegram = StatefulTelegram(unread)
    queue = ManualQueue()
    reads = ReadOps(telegram, queue, None, get_topic_name)
    enabled = [True]
    completed = []

    async def validate():
        return enabled[0]

    async def success():
        completed.append(True)

    await reads.mark_selective_as_read(1, None, message_id, validate, success)
    if disable:
        enabled[0] = False
    if new_message is not None:
        telegram.unread.append(new_message)
    await queue.run_next()
    return telegram, completed


async def test_selective_read_does_not_clear_a_prior_unapproved_message():
    telegram, completed = await run_selective([1, 2], 2)
    assert telegram.unread == [1, 2]
    assert not telegram.acknowledged and not completed


async def test_single_approved_unread_is_acknowledged_only_after_rpc():
    telegram, completed = await run_selective([2], 2)
    assert telegram.unread == []
    assert telegram.acknowledged == [2] and completed == [True]


async def test_selective_read_fails_closed_if_new_message_arrives():
    telegram, completed = await run_selective([2], 2, new_message=3)
    assert telegram.unread == [2, 3]
    assert not telegram.acknowledged and not completed


async def test_selective_read_rechecks_rule_after_queue_delay():
    telegram, completed = await run_selective([2], 2, disable=True)
    assert telegram.unread == [2]
    assert not telegram.acknowledged and not completed


def service_with_state(unread, user, rules=()):
    telegram = StatefulTelegram(unread)
    queue = ManualQueue()
    reads = ReadOps(telegram, queue, None, get_topic_name)
    adapter = AsyncMock()
    adapter.mark_automatic_as_read = AsyncMock(side_effect=reads.mark_automatic_as_read)
    repo = AsyncMock()
    repo.get_by_chat_and_topic.return_value = list(rules)
    service = make_service(rule_repo=repo, chat_repo=adapter, user=user)
    return service, telegram, queue


def event_for(message_id, text="ad", *, outgoing=False):
    return SystemEvent(
        type="message",
        text=text,
        chat_name="test",
        chat_id=1,
        message_model=make_message(id=message_id, text=text, is_outgoing=outgoing),
    )


async def test_real_decision_preserves_prior_human_unread():
    service, telegram, queue = service_with_state([1, 2], User(autoread_regex="ad"))
    event = event_for(2)
    await service.handle_new_message_event(event)
    await queue.run_next()
    assert telegram.unread == [1, 2]
    assert not event.is_read
    service.action_repo.add_log.assert_not_awaited()


async def test_real_decision_leaves_newer_human_unread():
    service, telegram, queue = service_with_state([1], User(autoread_regex="ad"))
    await service.handle_new_message_event(event_for(1))
    telegram.unread.append(2)
    await queue.run_next()
    assert telegram.unread == [1, 2]


async def test_multiple_approved_messages_never_bypass_unread_guard():
    service, telegram, queue = service_with_state([1, 2], User(autoread_regex="ad"))
    await service.handle_new_message_event(event_for(1))
    await service.handle_new_message_event(event_for(2))
    await queue.run_next()
    await queue.run_next()
    assert telegram.unread == [1, 2]


async def test_outgoing_message_cannot_clear_prior_unread():
    service, telegram, queue = service_with_state([1], User(autoread_self=True))
    await service.handle_new_message_event(event_for(2, outgoing=True))
    assert not queue.items
    assert telegram.unread == [1]


async def test_unknown_topic_scope_cannot_trigger_a_parent_rule():
    rule = make_rule(1, RuleType.AUTOREAD)
    service, telegram, queue = service_with_state([1], User(), [rule])
    event = event_for(1)
    event.scope_known = False
    await service.handle_new_message_event(event)
    assert not queue.items
    assert telegram.unread == [1]


async def test_blanket_read_is_discarded_when_rule_disabled_after_queue():
    rule = make_rule(1, RuleType.AUTOREAD)
    service, telegram, queue = service_with_state([1], User(), [rule])
    await service.handle_new_message_event(event_for(1))
    service.rule_repo.get_by_chat_and_topic.return_value = []
    await queue.run_next()
    assert telegram.unread == [1]
    service.action_repo.add_log.assert_not_awaited()


async def test_rule_disabled_during_peer_resolution_cancels_automatic_read():
    rule = make_rule(1, RuleType.AUTOREAD)
    service, telegram, queue = service_with_state([1], User(), [rule])
    await service.handle_new_message_event(event_for(1))
    resolve = telegram.get_input_entity

    async def disable_during_resolution(chat_id):
        service.rule_repo.get_by_chat_and_topic.return_value = []
        return await resolve(chat_id)

    telegram.get_input_entity = disable_during_resolution
    await queue.run_next()
    assert telegram.unread == [1]
    assert not telegram.acknowledged
    service.action_repo.add_log.assert_not_awaited()


async def test_retry_revalidates_after_flood_wait():
    rule = make_rule(1, RuleType.AUTOREAD)
    service, telegram, queue = service_with_state([1], User(), [rule])
    await service.handle_new_message_event(event_for(1))
    operation = queue.items.pop()
    original = telegram.send_read_acknowledge

    async def flood(*args, **kwargs):
        raise errors.FloodWaitError(request=None, capture=1)

    telegram.send_read_acknowledge = flood
    try:
        await operation()
    except errors.FloodWaitError:
        pass
    else:
        raise AssertionError("expected flood wait")
    service.rule_repo.get_by_chat_and_topic.return_value = []
    telegram.send_read_acknowledge = original
    await operation()
    assert telegram.unread == [1]
    service.action_repo.add_log.assert_not_awaited()


async def test_stalled_action_log_does_not_block_completed_read(monkeypatch):
    monkeypatch.setattr("src.infrastructure.action_logging.ACTION_LOG_TIMEOUT", 0.01)
    service, telegram, queue = service_with_state([1], User(autoread_regex="ad"))

    async def stalled(*_):
        await asyncio.Event().wait()

    service.action_repo.add_log.side_effect = stalled
    event = event_for(1)
    await service.handle_new_message_event(event)
    await asyncio.wait_for(queue.run_next(), timeout=0.5)
    assert telegram.acknowledged == [1]
    assert event.is_read


async def test_first_unread_uses_authoritative_boundary_without_writing():
    telegram = StatefulTelegram([4, 6])
    telegram.read_through = 2
    telegram.get_messages = AsyncMock(
        return_value=[
            SimpleNamespace(id=3, out=True),
            SimpleNamespace(id=4, out=False),
            SimpleNamespace(id=6, out=False),
        ]
    )
    reads = ReadOps(telegram, ManualQueue(), None, get_topic_name)
    result = await reads.first_unread_message_id(1, None)
    assert result == 4
    assert telegram.acknowledged == []
    assert telegram.get_messages.await_args.kwargs == {
        "limit": 50,
        "reverse": True,
        "min_id": 2,
        "reply_to": None,
    }


async def test_first_unread_is_discarded_if_read_state_changes_while_fetching():
    telegram = StatefulTelegram([4, 6])
    telegram.read_through = 2

    async def get_messages(*_args, **_kwargs):
        telegram.read_through = 4
        telegram.unread = [6]
        return [SimpleNamespace(id=4, out=False), SimpleNamespace(id=6, out=False)]

    telegram.get_messages = AsyncMock(side_effect=get_messages)
    reads = ReadOps(telegram, ManualQueue(), None, get_topic_name)
    assert await reads.first_unread_message_id(1, None) is None
    assert telegram.acknowledged == []


async def test_first_unread_uses_the_requested_forum_topic():
    state = SimpleNamespace(id=7, unread_count=1, top_message=8, read_inbox_max_id=4)
    telegram = AsyncMock(return_value=SimpleNamespace(topics=[state]))
    telegram.get_input_entity.return_value = types.InputPeerChannel(
        channel_id=10, access_hash=1
    )
    telegram.get_messages.return_value = [SimpleNamespace(id=8, out=False)]
    reads = ReadOps(telegram, ManualQueue(), None, get_topic_name)
    assert await reads.first_unread_message_id(-100, 7) == 8
    assert telegram.get_messages.await_args.kwargs["reply_to"] == 7
    assert all(
        isinstance(call.args[0], functions.messages.GetForumTopicsByIDRequest)
        for call in telegram.await_args_list
    )
    telegram.send_read_acknowledge.assert_not_awaited()
