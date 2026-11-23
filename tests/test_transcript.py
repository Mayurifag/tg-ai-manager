import asyncio
import json
import re
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from quart import request
from telethon import types

from src.adapters.telegram.event_handlers import EventHandlers
from src.application.interactors import ChatInteractor
from src.domain.models import Chat, ChatType, Message, SystemEvent
from src.web import create_app


class TranscriptStream(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.messages = [self.message(1, "Originally rendered")]
        self.history_reads = 0
        self.app = app = create_app()
        app.before_serving_funcs.clear()
        app.after_serving_funcs.clear()
        app.tg_adapter = SimpleNamespace(
            _retired=False,
            account_token="fixture",
            display_name="Fixture User",
            profile_avatar_id=None,
            is_connected=lambda: True,
            get_chat=self.get_chat,
            get_messages=self.get_messages,
        )
        app.chat_interactor = ChatInteractor(app.tg_adapter, None, None)
        app.rule_service = SimpleNamespace(get_effective_rules=self.empty)
        app.user_repo = SimpleNamespace(get_user=self.empty)

    @staticmethod
    def message(msg_id, text):
        return Message(msg_id, text, datetime.now(UTC), "Fixture", False)

    @staticmethod
    async def empty(*args):
        return {}

    @staticmethod
    async def get_chat(chat_id):
        return Chat(chat_id, "Fixture", 0, ChatType.GROUP)

    async def get_messages(self, chat_id, *, topic_id, offset_id):
        self.history_reads += 1
        return self.messages.copy()

    async def publish(self, msg_id, text, topic_id=None):
        message = self.message(msg_id, text)
        self.messages.insert(0, message)
        event = SystemEvent(
            "message",
            text,
            "Fixture",
            chat_id=42,
            topic_id=topic_id,
            message_model=message,
        )
        async with self.app.app_context():
            await self.app.chat_interactor.handle_event(event)
            await self.app.events.broadcast(event)

    async def open_transcript(self, client, topic_id=None):
        url = "/chat/42" if topic_id is None else f"/chat/42/topic/{topic_id}"
        page = await client.get(url)
        html = await page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200, html)
        self.assertIn("Originally rendered", html)
        self.assertNotIn("In the gap", html)
        snapshot = re.search(r'snapshot: "([^"]+)"', html).group(1)
        topic = "" if topic_id is None else f"&topic_id={topic_id}"
        return client.request(
            f"/api/events/stream?chat_id=42{topic}&snapshot={snapshot}&account=fixture",
            headers={"Accept": "text/event-stream"},
        )

    async def test_topic_catches_up_once_then_delivers_live_events(self):
        @self.app.context_processor
        async def during_render():
            if request.path == "/api/events/stream":
                await self.publish(3, "During snapshot render", topic_id=7)
            return {}

        async with self.app.test_app():
            client = self.app.test_client()
            connection = await self.open_transcript(client, topic_id=7)
            await self.publish(2, "In the gap", topic_id=7)
            async with connection as stream, asyncio.timeout(3):
                self.assertEqual(await stream.receive(), b": connected\n\n")
                snapshot = json.loads((await stream.receive()).removeprefix(b"data: "))
                self.assertEqual(snapshot["type"], "transcript_snapshot")
                self.assertIn("In the gap", snapshot["html"])
                self.assertNotIn("During snapshot render", snapshot["html"])
                live = json.loads((await stream.receive()).removeprefix(b"data: "))
                self.assertEqual(live["message_model"]["id"], 3)
                self.assertEqual(self.history_reads, 2)
                await stream.disconnect()

    async def test_current_transcript_opens_without_another_history_read(self):
        async with self.app.test_app():
            client = self.app.test_client()
            connection = await self.open_transcript(client)
            async with connection as stream, asyncio.timeout(3):
                self.assertEqual(await stream.receive(), b": connected\n\n")
                await self.publish(2, "Live without catch-up")
                live = json.loads((await stream.receive()).removeprefix(b"data: "))
                self.assertEqual(live["type"], "message")
                self.assertEqual(live["message_model"]["id"], 2)
                self.assertEqual(self.history_reads, 1)
                await stream.disconnect()

    async def test_edit_revision_in_transcript_and_live_edit(self):
        current = self.messages[0]
        current.edit_date = datetime(2026, 11, 23, 12, 2, tzinfo=UTC)
        older = self.message(current.id, "Older edit")
        older.edit_date = datetime(2026, 11, 23, 12, 1, tzinfo=UTC)
        original = types.UpdateEditMessage(
            types.Message(older.id, types.PeerChat(42), date=older.date),
            pts=123,
            pts_count=1,
        )
        parser = SimpleNamespace(
            _cache_message_chat=Mock(), _parse_message=AsyncMock(return_value=older)
        )
        handlers = EventHandlers(None, parser, Mock(), self.empty)
        handlers.add_event_listener(self.app.events.broadcast)
        async with self.app.test_app():
            page = await self.app.test_client().get("/chat/42")
            self.assertEqual(page.status_code, 200)
            self.assertIn(
                f'data-edit-date="{current.edit_date.isoformat()}"',
                await page.get_data(as_text=True),
            )
            client = self.app.events.subscribe(42)
            async with self.app.app_context():
                await handlers._handle_edited_message(
                    SimpleNamespace(
                        original_update=original,
                        message=original.message,
                        chat_id=42,
                        get_chat=AsyncMock(return_value=types.ChatEmpty(42)),
                    )
                )
            event = json.loads(client.queue.get_nowait())
            self.assertEqual(event["edit_pts"], 123)
            self.assertEqual(
                event["message_model"]["edit_date"], older.edit_date.isoformat()
            )
            self.assertIn(
                f'data-edit-date="{older.edit_date.isoformat()}"',
                event["rendered_html"],
            )
