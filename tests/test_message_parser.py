from types import SimpleNamespace

import pytest
from telethon.tl.types import PeerColor

from src.adapters.telegram.message_parser import MessageParser


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
