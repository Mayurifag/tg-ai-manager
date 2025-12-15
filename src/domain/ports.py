from abc import ABC, abstractmethod
from typing import List, Optional
from src.domain.models import Chat, Message

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

    @abstractmethod
    async def get_chat(self, chat_id: int) -> Optional[Chat]:
        pass

    @abstractmethod
    async def get_messages(self, chat_id: int, limit: int = 20, topic_id: Optional[int] = None, offset_id: int = 0) -> List[Message]:
        pass

    @abstractmethod
    async def get_forum_topics(self, chat_id: int, limit: int = 20) -> List[Chat]:
        pass
