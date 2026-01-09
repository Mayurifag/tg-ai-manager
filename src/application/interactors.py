from datetime import datetime
from typing import Awaitable, Callable, List, Optional

from src.domain.models import ActionLog, Chat, ChatType, Message, SystemEvent
from src.domain.ports import ActionRepository, ChatRepository, EventRepository


class ChatInteractor:
    def __init__(
        self,
        repository: ChatRepository,
        action_repo: ActionRepository,
        event_repo: EventRepository,
    ):
        self.repository = repository
        self.action_repo = action_repo
        self.event_repo = event_repo

    async def initialize(self):
        await self.repository.connect()

    async def shutdown(self):
        await self.repository.disconnect()

    async def get_recent_chats(self, limit: int = 20) -> List[Chat]:
        return await self.repository.get_chats(limit)

    async def get_chat(self, chat_id: int) -> Optional[Chat]:
        return await self.repository.get_chat(chat_id)

    async def get_forum_topics(self, chat_id: int) -> List[Chat]:
        return await self.repository.get_forum_topics(chat_id)

    async def get_chat_messages(
        self, chat_id: int, topic_id: Optional[int] = None, offset_id: int = 0
    ) -> List[Message]:
        return await self.repository.get_messages(
            chat_id, topic_id=topic_id, offset_id=offset_id
        )

    async def mark_chat_as_read(
        self, chat_id: int, topic_id: Optional[int] = None
    ) -> None:
        # 1. Perform Telegram Action
        await self.repository.mark_as_read(chat_id, topic_id)

        # 2. Fetch Chat Info for Log
        chat = await self.repository.get_chat(chat_id)
        chat_name = chat.name if chat else f"Chat {chat_id}"

        # 3. Determine Link and Action Name
        link = f"/chat/{chat_id}"
        action_name = "read_chat"

        if topic_id:
            # Fetch topic name for better logging
            t_name = await self.repository.get_topic_name(chat_id, topic_id)
            name_part = t_name if t_name else f"Topic {topic_id}"

            chat_name = f"{name_part} - {chat_name}"
            link = f"/chat/{chat_id}/topic/{topic_id}"
            action_name = "read_topic"

        elif chat and chat.type == ChatType.FORUM:
            link = f"/forum/{chat_id}"
            action_name = "read_forum"

        # 4. Create Log
        log = ActionLog(
            action=action_name,
            chat_id=chat_id,
            chat_name=chat_name,
            reason="manual",
            date=datetime.now(),
            link=link,
        )
        await self.action_repo.add_log(log)

    async def get_action_logs(self, limit: int = 50) -> List[ActionLog]:
        return await self.action_repo.get_logs(limit)

    async def get_media_path(self, chat_id: int, message_id: int) -> Optional[str]:
        return await self.repository.download_media(chat_id, message_id)

    async def get_chat_avatar(self, chat_id: int) -> Optional[str]:
        return await self.repository.get_chat_avatar(chat_id)

    async def subscribe_to_events(
        self, callback: Callable[[SystemEvent], Awaitable[None]]
    ):
        async def wrapped_callback(event: SystemEvent):
            # Persist event to Valkey
            await self.event_repo.add_event(event)
            # Forward to original callback (SSE broadcast)
            await callback(event)

        self.repository.add_event_listener(wrapped_callback)

    async def get_recent_events(self, limit: int = 10) -> List[SystemEvent]:
        """Fetches recent system events from persistence layer."""
        return await self.event_repo.get_recent_events(limit)

    async def run_storage_maintenance(self):
        """Triggers the repository storage cleanup."""
        await self.repository.run_storage_maintenance()
