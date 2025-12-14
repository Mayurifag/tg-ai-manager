from typing import List, Optional
from src.domain.ports import ChatRepository
from src.domain.models import Chat, Message

class ChatService:
    def __init__(self, repository: ChatRepository):
        self.repository = repository

    async def initialize(self):
        await self.repository.connect()

    async def shutdown(self):
        await self.repository.disconnect()

    async def get_recent_chats(self, limit: int = 10) -> List[Chat]:
        return await self.repository.get_chats(limit)

    async def get_forum_topics(self, chat_id: int) -> List[Chat]:
        return await self.repository.get_forum_topics(chat_id)

    async def get_chat_messages(self, chat_id: int, topic_id: Optional[int] = None) -> List[Message]:
        return await self.repository.get_messages(chat_id, topic_id=topic_id)
