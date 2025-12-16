from typing import List, Optional, Callable, Awaitable
from collections import deque
from src.domain.ports import ChatRepository
from src.domain.models import Chat, Message, SystemEvent

class ChatInteractor:
    def __init__(self, repository: ChatRepository):
        self.repository = repository
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

    async def get_media_path(self, chat_id: int, message_id: int) -> Optional[str]:
        return await self.repository.download_media(chat_id, message_id)

    async def subscribe_to_events(self, callback: Callable[[SystemEvent], Awaitable[None]]):
        async def wrapped_callback(event: SystemEvent):
            self.recent_events.appendleft(event)
            await callback(event)
        self.repository.add_event_listener(wrapped_callback)

    def get_recent_events(self) -> List[SystemEvent]:
        return list(self.recent_events)
