from datetime import datetime, timezone
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from telethon.tl import types

from src.adapters.telegram.message_parser import MessageParser
from src.adapters.telethon_mappers import format_message_preview
from src.domain.models import Chat, ChatType, Message, SystemEvent
from src.rules.sync import sync_rules_from_remote
from src.users.models import User
from src.web import create_app


@pytest.mark.parametrize("kind", ["service", "poll", "text", "audio"])
async def test_chat_cards_render_telegram_previews_as_plain_text(kind):
    payload = '<a href="javascript:alert(1)" onclick="alert(2)">bad</a>'
    sender = types.User(id=4, first_name=payload)
    if kind == "service":
        message = types.MessageService(
            id=1,
            peer_id=types.PeerChat(1),
            action=types.MessageActionChatEditTitle(title=payload),
        )
    elif kind == "poll":
        poll = types.Poll(
            id=1,
            question=types.TextWithEntities(text=payload, entities=[]),
            answers=[],
            hash=0,
        )
        message = SimpleNamespace(
            message="",
            media=types.MessageMediaPoll(poll=poll, results=types.PollResults()),
            sender=sender,
        )
    elif kind == "audio":
        document = types.Document(
            id=1,
            access_hash=1,
            file_reference=b"",
            date=None,
            mime_type="audio/mpeg",
            size=10,
            dc_id=1,
            attributes=[types.DocumentAttributeAudio(duration=1, title=payload)],
        )
        message = SimpleNamespace(
            message="",
            media=types.MessageMediaDocument(document=document),
            sender=sender,
        )
    else:
        message = SimpleNamespace(message=payload, media=None, sender=sender)

    chat = Chat(
        id=1,
        name="Example",
        unread_count=0,
        type=ChatType.GROUP,
        last_message_preview=format_message_preview(message, ChatType.GROUP),
    )
    app = create_app()
    for name, context in (
        ("partials/chat_cards.html.j2", {"chats": [chat]}),
        ("partials/chat_card_wrapper.html.j2", {"chat": chat}),
    ):
        rendered = await app.jinja_env.get_template(name).render_async(**context)
        assert payload not in rendered
        assert "&lt;a href=&#34;javascript:alert(1)&#34;" in rendered


async def test_service_message_metadata_does_not_become_html():
    message = types.MessageService(
        id=1,
        peer_id=types.PeerChat(1),
        action=types.MessageActionChatEditTitle(title="<b>literal</b>"),
    )
    parsed = await MessageParser(None, None)._parse_message(message)
    assert "<b>literal</b>" not in parsed.text
    assert "&lt;b&gt;literal&lt;/b&gt;" in parsed.text


@pytest.mark.parametrize("chat_type", [ChatType.GROUP, ChatType.TOPIC])
async def test_unread_card_has_separate_navigation_and_read_controls(chat_type):
    class InteractiveParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.anchors = 0
            self.nested = False
            self.links = []
            self.buttons = 0

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.anchors += 1
                self.links.append(dict(attrs).get("href"))
            elif tag == "button":
                self.buttons += 1
                self.nested |= self.anchors > 0

        def handle_endtag(self, tag):
            if tag == "a":
                self.anchors -= 1

    chat = Chat(id=7, name="Test", unread_count=1, type=chat_type)
    app = create_app()
    template = (
        "forum/topic_card_partial.html.j2"
        if chat_type == ChatType.TOPIC
        else "partials/chat_card_wrapper.html.j2"
    )
    rendered = await app.jinja_env.get_template(template).render_async(
        chat=chat, topic=chat, parent_id=100
    )
    parser = InteractiveParser()
    parser.feed(rendered)
    assert not parser.nested
    assert parser.buttons == 1
    assert parser.links == (
        ["/chat/100/topic/7"] if chat_type == ChatType.TOPIC else ["/chat/7"]
    )


async def test_http_sync_error_does_not_log_request_url_or_secret():
    request = httpx.Request(
        "GET",
        "https://example.test/rules?token=query-secret",
        headers={"Authorization": "Bearer header-secret"},
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(403)),
        headers={"Authorization": "Bearer header-secret"},
    )
    users = AsyncMock()
    users.get_user.return_value = User(
        api_id=1, api_hash="synthetic", session_string="synthetic-session"
    )
    with (
        patch("src.rules.sync.httpx.AsyncClient", return_value=client),
        patch("src.rules.sync.logger") as logger,
    ):
        await sync_rules_from_remote(
            str(request.url), AsyncMock(), users, expected_account_id=42
        )
    logger.warning.assert_called_once_with(
        "rules_sync_failed", category="http_status", status=403
    )
    output = str(logger.mock_calls)
    assert "query-secret" not in output
    assert "header-secret" not in output
    assert "example.test" not in output


async def test_sse_skips_rendering_without_connected_clients():
    from src.web.sse import EventBroadcaster

    broadcaster = EventBroadcaster()
    event = SystemEvent(
        type="message",
        text="hi",
        chat_name="chat",
        message_model=Message(
            id=1,
            text="hi",
            date=datetime.now(timezone.utc),
            sender_name="x",
            is_outgoing=False,
        ),
    )
    # No application context exists: rendering would fail if attempted.
    await broadcaster.broadcast(event)
