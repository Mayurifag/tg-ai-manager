from typing import List
from src.domain.ports import ChatRepository
from src.domain.models import Chat

class ChatService:
    def __init__(self, repository: ChatRepository):
        self.repository = repository

    async def initialize(self):
        await self.repository.connect()

    async def shutdown(self):
        await self.repository.disconnect()

    async def get_recent_chats(self, limit: int = 10) -> List[Chat]:
        return await self.repository.get_chats(limit)