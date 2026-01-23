from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.domain.models import ActionLog, Chat, ChatType, Message, SystemEvent
from src.domain.ports import ActionRepository, ChatRepository, EventRepository
from src.infrastructure.queue_service import QueueService


class ChatInteractor:
    def __init__(
        self,
        repository: ChatRepository,
        action_repo: ActionRepository,
        event_repo: EventRepository,
        queue_service: QueueService,
    ):
        self.repository = repository
        self.action_repo = action_repo
        self.event_repo = event_repo
        self.queue_service = queue_service

    async def initialize(self):
        await self.repository.connect()
        await self.queue_service.connect()

    async def shutdown(self):
        await self.repository.disconnect()
        await self.queue_service.close()

    async def get_recent_chats(self, limit: int = 20) -> List[Chat]:
        return await self.repository.get_chats(limit)

    async def get_chat(self, chat_id: int) -> Optional[Chat]:
        return await self.repository.get_chat(chat_id)

    async def get_recent_authors(self, chat_id: int) -> List[Dict[str, Any]]:
        return await self.repository.get_recent_authors(chat_id)

    async def get_forum_topics(self, chat_id: int) -> List[Chat]:
        return await self.repository.get_forum_topics(chat_id)

    async def get_message_by_id(self, chat_id: int, msg_id: int) -> Optional[Message]:
        messages = await self.repository.get_messages(chat_id, ids=[msg_id])
        if messages:
            return messages[0]
        return None

    async def get_single_message(self, chat_id: int, msg_id: int) -> Optional[Message]:
        # Alias for get_message_by_id
        return await self.get_message_by_id(chat_id, msg_id)

    async def get_chat_messages(
        self, chat_id: int, topic_id: Optional[int] = None, offset_id: int = 0
    ) -> List[Message]:
        raw_messages = await self.repository.get_messages(
            chat_id, topic_id=topic_id, offset_id=offset_id
        )
        return self._group_messages_into_albums(raw_messages)

    def _group_messages_into_albums(self, messages: List[Message]) -> List[Message]:
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

                current_msg.text = final_caption
                current_msg.album_parts = sorted(album_parts, key=lambda m: m.id)

                grouped_messages.append(current_msg)
                i = j
            else:
                grouped_messages.append(current_msg)
                i += 1

        return grouped_messages

    async def mark_chat_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int] = None,
        max_id: Optional[int] = None,
    ) -> None:
        # Enqueue Job
        await self.queue_service.enqueue_mark_read(chat_id, topic_id, max_id)

        # Optimistic logging
        chat = await self.repository.get_chat(chat_id)
        chat_name = chat.name if chat else f"Chat {chat_id}"

        link = f"/chat/{chat_id}"
        action_name = "read_chat_queued"

        if topic_id:
            t_name = await self.repository.get_topic_name(chat_id, topic_id)
            name_part = t_name if t_name else f"Topic {topic_id}"

            chat_name = f"{name_part} - {chat_name}"
            link = f"/chat/{chat_id}/topic/{topic_id}"
            action_name = "read_topic_queued"

        elif chat and chat.type == ChatType.FORUM:
            link = f"/forum/{chat_id}"
            action_name = "read_forum_queued"

        log = ActionLog(
            action=action_name,
            chat_id=chat_id,
            chat_name=chat_name,
            reason="manual",
            date=datetime.now(),
            link=link,
        )
        await self.action_repo.add_log(log)

    async def toggle_reaction(self, chat_id: int, msg_id: int, emoji: str) -> bool:
        # Enqueue Job
        await self.queue_service.enqueue_reaction(chat_id, msg_id, emoji)
        return True

    async def get_action_logs(self, limit: int = 50) -> List[ActionLog]:
        return await self.action_repo.get_logs(limit)

    async def get_media_path(
        self, chat_id: int, message_id: int, size_type: str = "preview"
    ) -> Optional[str]:
        return await self.repository.download_media(chat_id, message_id, size_type)

    async def get_chat_avatar(self, chat_id: int) -> Optional[str]:
        return await self.repository.get_chat_avatar(chat_id)

    async def subscribe_to_events(
        self, callback: Callable[[SystemEvent], Awaitable[None]]
    ):
        async def wrapped_callback(event: SystemEvent):
            await self.event_repo.add_event(event)
            await callback(event)

        self.repository.add_event_listener(wrapped_callback)

    async def get_recent_events(self, limit: int = 10) -> List[SystemEvent]:
        return await self.event_repo.get_recent_events(limit)

    async def run_storage_maintenance(self):
        await self.repository.run_storage_maintenance()

    async def get_self_premium_status(self) -> bool:
        return await self.repository.get_self_premium_status()
