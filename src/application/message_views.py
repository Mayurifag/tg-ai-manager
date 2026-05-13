from dataclasses import replace

from src.domain.models import Message


def group_messages_into_albums(messages: list[Message]) -> list[Message]:
    if not messages:
        return []

    grouped_messages = []
    i = 0
    while i < len(messages):
        current_msg = messages[i]

        if current_msg.grouped_id:
            album_parts = [current_msg]
            j = i + 1
            while j < len(messages):
                next_msg = messages[j]
                if next_msg.grouped_id == current_msg.grouped_id:
                    album_parts.append(next_msg)
                    j += 1
                else:
                    break

            final_caption = current_msg.text
            if not final_caption:
                for part in album_parts:
                    if part.text:
                        final_caption = part.text
                        break

            grouped_messages.append(
                replace(
                    current_msg,
                    text=final_caption,
                    album_parts=sorted(album_parts, key=lambda m: m.id),
                )
            )
            i = j
        else:
            grouped_messages.append(current_msg)
            i += 1

    return grouped_messages
