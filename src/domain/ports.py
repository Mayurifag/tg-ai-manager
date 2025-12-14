from abc import ABC, abstractmethod
from typing import List
from src.domain.models import Chat

class ChatRepository(ABC):
    @abstractmethod
    async def connect(self):
        pass

    @abstractmethod
    async def disconnect(self):
        pass

    @abstractmethod
    async def get_chats(self, limit: int) -> List[Chat]:
        pass