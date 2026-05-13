from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.adapters.telegram.event_handlers import EventHandlers
from src.domain.models import Message, SystemEvent


class FakeParser:
    def __init__(self):
        self._msg_id_to_chat_id = {}

    def _cache_message_chat(self, msg_id: int, chat_id: int):
        self._msg_id_to_chat_id[msg_id] = chat_id

    def _extract_topic_id(self, message):
        return message.reply_to.reply_to_top_id

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
            reply_to=SimpleNamespace(reply_to_top_id=7),
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
    get_topic_name.assert_awaited_once_with(100, 7)
