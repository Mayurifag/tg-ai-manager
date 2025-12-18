from typing import List, Optional, Callable, Awaitable
from collections import deque
from datetime import datetime
from src.domain.ports import ChatRepository, ActionRepository
from src.domain.models import Chat, Message, SystemEvent, ActionLog, ChatType

class ChatInteractor:
    def __init__(self, repository: ChatRepository, action_repo: ActionRepository):
        self.repository = repository
        self.action_repo = action_repo
        self.recent_events = deque(maxlen=5)

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

    async def get_chat_messages(self, chat_id: int, topic_id: Optional[int] = None, offset_id: int = 0) -> List[Message]:
        return await self.repository.get_messages(chat_id, topic_id=topic_id, offset_id=offset_id)

    async def mark_chat_as_read(self, chat_id: int, topic_id: Optional[int] = None) -> None:
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
            link=link
        )
        await self.action_repo.add_log(log)

    async def get_action_logs(self, limit: int = 50) -> List[ActionLog]:
        return await self.action_repo.get_logs(limit)

    async def get_media_path(self, chat_id: int, message_id: int) -> Optional[str]:
        return await self.repository.download_media(chat_id, message_id)

    async def subscribe_to_events(self, callback: Callable[[SystemEvent], Awaitable[None]]):
        async def wrapped_callback(event: SystemEvent):
            self.recent_events.appendleft(event)
            await callback(event)
        self.repository.add_event_listener(wrapped_callback)

    def get_recent_events(self) -> List[SystemEvent]:
        return list(self.recent_events)
