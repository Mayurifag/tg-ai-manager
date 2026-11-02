from dataclasses import replace

from src.domain.models import Message


def group_messages_into_albums(messages: list[Message]) -> list[Message]:
    # Album members may be interleaved with other messages. Keep the album at
    # its newest member's position without consuming those unrelated messages.
    grouped_messages: list[Message] = []
    albums: dict[int, list[Message]] = {}
    for message in messages:
        if message.grouped_id:
            if message.grouped_id in albums:
                albums[message.grouped_id].append(message)
                continue
            albums[message.grouped_id] = [message]
        grouped_messages.append(message)

    return [
        replace(
            message,
            text=next(
                (part.text for part in albums[message.grouped_id] if part.text), ""
            ),
            album_parts=sorted(albums[message.grouped_id], key=lambda part: part.id),
        )
        if message.grouped_id
        else message
        for message in grouped_messages
    ]
