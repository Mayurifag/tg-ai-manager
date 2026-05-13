from datetime import datetime

from src.application.message_views import group_messages_into_albums
from src.domain.models import Message


def make_message(id: int, text: str = "", grouped_id: int | None = None) -> Message:
    return Message(
        id=id,
        text=text,
        date=datetime.now(),
        sender_name="Sender",
        is_outgoing=False,
        grouped_id=grouped_id,
    )


def test_group_messages_into_albums_does_not_mutate_source_messages():
    first = make_message(2, grouped_id=10)
    second = make_message(1, text="caption", grouped_id=10)

    grouped = group_messages_into_albums([first, second])

    assert first.text == ""
    assert first.album_parts is None
    assert grouped[0] is not first
    assert grouped[0].text == "caption"
    assert [part.id for part in grouped[0].album_parts] == [1, 2]
