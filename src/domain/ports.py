from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.domain.models import ActionLog, Chat, Message, SystemEvent


class ChatRepository(ABC):
    @abstractmethod
    def is_connected(self) -> bool:
        """Checks if the client is currently connected and authorized."""
        pass

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
    async def get_all_unread_chats(self) -> List[Chat]:
        """Returns all chats with unread messages."""
        pass

    @abstractmethod
    async def get_chat(self, chat_id: int) -> Optional[Chat]:
        pass

    @abstractmethod
    async def get_messages(
        self,
        chat_id: int,
        limit: int = 20,
        topic_id: Optional[int] = None,
        offset_id: int = 0,
        ids: Optional[List[int]] = None,
    ) -> List[Message]:
        pass

    @abstractmethod
    async def get_recent_authors(
        self, chat_id: int, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Fetches distinct authors from recent messages."""
        pass

    @abstractmethod
    async def get_forum_topics(self, chat_id: int, limit: int = 20) -> List[Chat]:
        pass

    @abstractmethod
    async def get_unread_topics(self, chat_id: int) -> List[Chat]:
        """Returns all topics with unread messages in a forum."""
        pass

    @abstractmethod
    async def get_topic_name(self, chat_id: int, topic_id: int) -> Optional[str]:
        """Get the name of a specific forum topic."""
        pass

    @abstractmethod
    async def download_media(
        self, chat_id: int, message_id: int, size_type: str = "preview"
    ) -> Optional[str]:
        """Downloads the media for a message. size_type: 'preview' or 'full'."""
        pass

    @abstractmethod
    async def get_chat_avatar(self, chat_id: int) -> Optional[str]:
        """Downloads or retrieves cached avatar for a chat."""
        pass

    @abstractmethod
    def add_event_listener(self, callback: Callable[[SystemEvent], Awaitable[None]]):
        """Register a callback for system events."""
        pass

    @abstractmethod
    async def mark_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int] = None,
        max_id: Optional[int] = None,
    ) -> None:
        """Marks the chat or specific topic as read. max_id is the ID up to which to read."""
        pass

    @abstractmethod
    async def send_reaction(self, chat_id: int, msg_id: int, emoji: str) -> bool:
        """Toggles a reaction on a message."""
        pass

    @abstractmethod
    async def get_self_premium_status(self) -> bool:
        """Checks if the current session user is premium (fetches fresh)."""
        pass

    @abstractmethod
    async def run_storage_maintenance(self) -> None:
        """Prunes cached files based on size limits (LRU/Oldest-first)."""
        pass


class ActionRepository(ABC):
    @abstractmethod
    async def add_log(self, log: ActionLog) -> None:
        pass

    @abstractmethod
    async def get_logs(self, limit: int = 50) -> List[ActionLog]:
        pass


class EventRepository(ABC):
    @abstractmethod
    async def add_event(self, event: SystemEvent) -> None:
        pass

    @abstractmethod
    async def get_recent_events(self, limit: int = 10) -> List[SystemEvent]:
        pass
