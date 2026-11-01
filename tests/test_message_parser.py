from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from telethon.tl import types
from telethon.tl.types import PeerColor

from src.adapters.telegram.message_parser import MessageParser
from src.web import create_app


@pytest.mark.parametrize(
    ("peer_color", "sender_id", "expected"),
    [
        (PeerColor(background_emoji_id=123), 8, "#faa774"),
        (PeerColor(background_emoji_id=123), -8, "#faa774"),
        (PeerColor(color=0), 8, "#e17076"),
        (PeerColor(color=6), 8, "#ee7aae"),
        (PeerColor(color=8), 6, "#faa774"),
        (None, 8, "#faa774"),
        (None, 0, "#e17076"),
    ],
)
def test_sender_color_uses_explicit_color_or_sender_id(peer_color, sender_id, expected):
    parser = MessageParser(None, None)
    sender = SimpleNamespace(color=peer_color)

    assert parser._get_sender_color(sender, sender_id) == expected


def test_sender_color_without_sender():
    parser = MessageParser(None, None)

    assert parser._get_sender_color(None, -8) == "#faa774"


def test_reaction_chosen_order_zero_is_selected():
    reactions = types.MessageReactions(
        results=[
            types.ReactionCount(
                reaction=types.ReactionEmoji(emoticon="👍"), count=2, chosen_order=0
            ),
            types.ReactionCount(reaction=types.ReactionEmoji(emoticon="👎"), count=1),
        ]
    )
    parsed = MessageParser(None, None)._extract_reactions(reactions)
    assert [reaction.is_chosen for reaction in parsed] == [True, False]


def telegram_message(media=None, **kwargs):
    return SimpleNamespace(
        id=1,
        date=datetime.now(timezone.utc),
        message=kwargs.get("message", ""),
        entities=kwargs.get("entities", []),
        media=media,
        from_id=None,
        sender=None,
        reply_to=None,
        fwd_from=kwargs.get("fwd_from"),
    )


async def test_document_and_forward_are_rendered_safely():
    document = types.Document(
        id=1,
        access_hash=1,
        file_reference=b"",
        date=None,
        mime_type="application/pdf",
        size=1024,
        dc_id=1,
        attributes=[
            types.DocumentAttributeFilename(
                file_name="<img src=x onerror=alert(1)>.pdf"
            )
        ],
    )
    message = await MessageParser(None, None)._parse_message(
        telegram_message(
            types.MessageMediaDocument(document=document),
            fwd_from=SimpleNamespace(from_name="<script>alert(2)</script>"),
        )
    )
    assert message.is_document and message.file_size == 1024
    assert "<img" not in message.get_preview_text()
    app = create_app()
    assert app.jinja_env.autoescape("chat/messages_partial.html.j2")
    template = app.jinja_env.get_template("chat/messages_partial.html.j2")
    html = await template.render_async(
        messages=[message], chat_id=-100, current_user=None
    )
    assert 'href="/media/-100/1/full"' in html
    assert "<img src=x onerror=" not in html
    assert "<script>alert(2)</script>" not in html
    assert "&lt;img src=x onerror=" in html


async def test_channel_post_uses_marked_channel_id_for_avatar():
    channel = types.Channel(
        id=42, title="Channel", photo=types.ChatPhotoEmpty(), date=None, broadcast=True
    )
    msg = telegram_message()
    msg.post = True
    msg.chat = channel
    media = SimpleNamespace(
        _get_chat_image=AsyncMock(return_value="/media/avatar/-1000000000042")
    )
    parsed = await MessageParser(None, media)._parse_message(msg)
    assert parsed.sender_name == "Channel"
    assert parsed.sender_id == -1000000000042
    media._get_chat_image.assert_awaited_once_with(channel, -1000000000042)


async def test_poll_options_and_webpage_are_not_treated_as_images():
    text = lambda value: types.TextWithEntities(text=value, entities=[])  # noqa: E731
    poll = types.Poll(
        id=1,
        question=text("Question?"),
        hash=0,
        answers=[types.PollAnswer(text=text("First"), option=b"1")],
    )
    parser = MessageParser(None, None)
    parsed_poll = await parser._parse_message(
        telegram_message(types.MessageMediaPoll(poll=poll, results=types.PollResults()))
    )
    assert parsed_poll.is_poll and parsed_poll.poll_options == ["First"]
    webpage = types.WebPage(
        id=1,
        url="https://example.org/path",
        display_url="example.org",
        hash=0,
        title="Example",
    )
    parsed_link = await parser._parse_message(
        telegram_message(types.MessageMediaWebPage(webpage=webpage))
    )
    assert parsed_link.webpage_url == "https://example.org/path"
    assert not parsed_link.is_photo
    webpage.url = "javascript:alert(1)"
    assert (
        await parser._parse_message(
            telegram_message(types.MessageMediaWebPage(webpage=webpage))
        )
    ).webpage_url is None
    webpage.url = "https://[invalid"
    assert (
        await parser._parse_message(
            telegram_message(types.MessageMediaWebPage(webpage=webpage))
        )
    ).webpage_url is None
