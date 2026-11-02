from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telethon import types

from src.adapters.telegram.event_handlers import EventHandlers
from src.adapters.telegram.message_parser import MessageParser
from src.domain.models import Message, SystemEvent


class FakeParser:
    def __init__(self):
        self._msg_id_to_chat_id = {}

    def _cache_message_chat(self, msg_id: int, chat_id: int):
        self._msg_id_to_chat_id[msg_id] = chat_id

    def _extract_topic_id(self, message):
        return MessageParser(None, None)._extract_topic_id(message)

    async def _parse_message(self, message, chat_id=None):
        return Message(
            id=message.id,
            text="hello",
            date=datetime.now(),
            sender_name="Sender",
            is_outgoing=False,
        )


class FakeNewMessageEvent:
    chat_id = 100

    def __init__(self, forum: bool):
        self._forum = forum
        self.message = SimpleNamespace(
            id=42,
            reply_to=SimpleNamespace(reply_to_top_id=7, forum_topic=True),
        )

    async def get_chat(self):
        return SimpleNamespace(title="Chat", forum=self._forum)


async def test_new_message_ignores_reply_top_id_in_non_forum_chat():
    parser = FakeParser()
    get_topic_name = AsyncMock(return_value="Topic")
    handler = EventHandlers(None, parser, None, get_topic_name)
    received: list[SystemEvent] = []

    async def listener(event: SystemEvent):
        received.append(event)

    handler.add_event_listener(listener)

    await handler._handle_new_message(FakeNewMessageEvent(forum=False))

    assert received[0].topic_id is None
    assert received[0].topic_name is None
    assert received[0].link == "/chat/100#msg-42"
    get_topic_name.assert_not_awaited()


async def test_new_message_uses_reply_top_id_in_forum_chat():
    parser = FakeParser()
    get_topic_name = AsyncMock(return_value="Topic")
    handler = EventHandlers(None, parser, None, get_topic_name)
    received: list[SystemEvent] = []

    async def listener(event: SystemEvent):
        received.append(event)

    handler.add_event_listener(listener)

    await handler._handle_new_message(FakeNewMessageEvent(forum=True))

    assert received[0].topic_id == 7
    assert received[0].topic_name == "Topic"
    assert received[0].link == "/chat/100/topic/7#msg-42"
    get_topic_name.assert_awaited_once_with(100, 7)


async def test_forum_root_reply_and_general_use_distinct_topic_ids():
    parser = FakeParser()
    handler = EventHandlers(None, parser, None, AsyncMock(return_value="Topic"))
    received = []
    handler.add_event_listener(AsyncMock(side_effect=received.append))
    event = FakeNewMessageEvent(forum=True)
    event.message.reply_to = types.MessageReplyHeader(
        forum_topic=True, reply_to_msg_id=11
    )
    await handler._handle_new_message(event)
    event.message.reply_to = types.MessageReplyHeader(reply_to_msg_id=77)
    await handler._handle_new_message(event)
    assert [item.topic_id for item in received] == [11, 1]
    assert received[0].link == "/chat/100/topic/11#msg-42"


async def test_topic_edit_invalidates_cached_name_before_event_is_sent():
    parser = FakeParser()
    invalidate = MagicMock()
    lookup = AsyncMock(return_value="Renamed")
    handler = EventHandlers(
        None, parser, None, lookup, invalidate_topic_name_fn=invalidate
    )
    received = []
    handler.add_event_listener(AsyncMock(side_effect=received.append))
    event = FakeNewMessageEvent(forum=True)
    event.message.action = types.MessageActionTopicEdit(title="Renamed")
    await handler._handle_new_message(event)
    invalidate.assert_called_once_with(100, 7)
    lookup.assert_awaited_once_with(100, 7)
    assert received[0].topic_name == "Renamed"


async def test_unknown_chat_or_topic_is_not_authorized_for_automation():
    parser = FakeParser()
    handler = EventHandlers(None, parser, None, AsyncMock())
    received = []
    handler.add_event_listener(AsyncMock(side_effect=received.append))
    event = FakeNewMessageEvent(forum=True)
    event.message.reply_to = types.MessageReplyHeader(forum_topic=True)
    await handler._handle_new_message(event)
    assert received[-1].topic_id is None
    assert not received[-1].scope_known

    event.get_chat = AsyncMock(side_effect=RuntimeError("temporarily unavailable"))
    await handler._handle_new_message(event)
    assert not received[-1].scope_known


async def test_edited_message_invalidates_and_replaces_media_reference():
    parser = FakeParser()
    media = MagicMock()
    event = FakeNewMessageEvent(forum=False)
    handler = EventHandlers(None, parser, media, AsyncMock())
    received = []
    handler.add_event_listener(AsyncMock(side_effect=received.append))

    await handler._handle_edited_message(event)

    media.invalidate_message.assert_called_once_with(100, 42)
    media.remember_messages.assert_called_once_with(100, [event.message])
    assert received[0].link == "/chat/100#msg-42"


async def test_deleted_messages_invalidate_each_media_reference():
    parser = FakeParser()
    media = MagicMock()
    client = SimpleNamespace(get_entity=AsyncMock(return_value=None))
    event = SimpleNamespace(chat_id=100, deleted_ids=[42, 43])
    handler = EventHandlers(client, parser, media, AsyncMock())

    await handler._handle_deleted_message(event)

    assert media.invalidate_message.call_count == 2
    media.invalidate_message.assert_any_call(100, 42)
    media.invalidate_message.assert_any_call(100, 43)


async def test_channel_deletions_do_not_collide_with_account_scoped_ids():
    parser = MessageParser(None, None)
    parser._cache_message_chat(42, 123)
    parser._cache_message_chat(42, -1000000000042)
    parser._cache_message_chat(42, -1000000000043)
    media = MagicMock()
    handler = EventHandlers(
        SimpleNamespace(get_entity=AsyncMock(return_value=None)),
        parser,
        media,
        AsyncMock(),
    )
    received = []
    handler.add_event_listener(AsyncMock(side_effect=received.append))
    await handler._handle_deleted_message(
        SimpleNamespace(chat_id=-1000000000042, deleted_ids=[42, 43])
    )
    await handler._handle_deleted_message(
        SimpleNamespace(chat_id=None, deleted_ids=[42])
    )
    assert [(event.chat_id, event.message_model.id) for event in received] == [
        (-1000000000042, 42),
        (-1000000000042, 43),
        (123, 42),
    ]
    assert media.invalidate_message.call_count == 3


async def test_peerless_deletions_resolve_each_id_and_resync_unknown():
    parser = MessageParser(None, None)
    parser._cache_message_chat(10, 123)
    parser._cache_message_chat(20, 456)
    handler = EventHandlers(
        SimpleNamespace(get_entity=AsyncMock(return_value=None)),
        parser,
        MagicMock(),
        AsyncMock(),
    )
    received = []
    handler.add_event_listener(AsyncMock(side_effect=received.append))
    await handler._handle_deleted_message(
        SimpleNamespace(chat_id=None, deleted_ids=[10, 20, 30])
    )
    assert [(event.chat_id, event.message_model.id) for event in received[:2]] == [
        (123, 10),
        (456, 20),
    ]
    assert received[2].type == "resync_required" and received[2].chat_id is None
