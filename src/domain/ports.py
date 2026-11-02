from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.domain.models import (
    ActionLog,
    Chat,
    ForumTopicCursor,
    ForumTopicPage,
    MediaContent,
    Message,
    MessageContext,
    SystemEvent,
)


class ChatConnectionPort(ABC):
    @abstractmethod
    def is_connected(self) -> bool:
        pass

    @abstractmethod
    async def connect(self):
        pass

    @abstractmethod
    async def disconnect(self):
        pass


class ChatQueryPort(ABC):
    @abstractmethod
    async def get_chats(self) -> List[Chat]:
        pass

    @abstractmethod
    async def get_all_unread_chats(
        self, chat_ids: Optional[List[int]] = None
    ) -> List[Chat]:
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
        after_id: Optional[int] = None,
    ) -> List[Message]:
        pass

    @abstractmethod
    async def get_message_context(
        self, chat_id: int, message_id: int
    ) -> Optional[MessageContext]:
        pass

    @abstractmethod
    async def can_selectively_read(
        self, chat_id: int, topic_id: Optional[int], message_id: int
    ) -> bool:
        pass

    @abstractmethod
    async def first_unread_message_id(
        self, chat_id: int, topic_id: Optional[int]
    ) -> Optional[int]:
        pass

    @abstractmethod
    async def get_recent_authors(
        self, chat_id: int, limit: int = 100
    ) -> List[Dict[str, Any]]:
        pass


class ForumQueryPort(ABC):
    @abstractmethod
    async def get_forum_topics(self, chat_id: int, limit: int = 20) -> List[Chat]:
        pass

    @abstractmethod
    async def get_forum_topics_page(
        self, chat_id: int, cursor: Optional[ForumTopicCursor] = None
    ) -> ForumTopicPage:
        pass

    @abstractmethod
    async def get_forum_topic(self, chat_id: int, topic_id: int) -> Optional[Chat]:
        pass

    @abstractmethod
    async def get_unread_topics(
        self, chat_id: int, topic_ids: Optional[List[int]] = None
    ) -> List[Chat]:
        pass

    @abstractmethod
    async def get_topic_name(self, chat_id: int, topic_id: int) -> Optional[str]:
        pass


class TelegramMediaPort(ABC):
    @abstractmethod
    async def download_media(
        self, chat_id: int, message_id: int, size_type: str = "preview"
    ) -> Optional[MediaContent]:
        pass

    @abstractmethod
    async def get_chat_avatar(self, chat_id: int) -> Optional[bytes]:
        pass

    @abstractmethod
    async def run_storage_maintenance(self) -> None:
        pass


class TelegramEventPort(ABC):
    @abstractmethod
    def add_event_listener(self, callback: Callable[[SystemEvent], Awaitable[None]]):
        pass


class TelegramWritePort(ABC):
    @abstractmethod
    async def mark_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int] = None,
        max_id: Optional[int] = None,
    ) -> None:
        pass

    @abstractmethod
    async def mark_automatic_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int],
        max_id: int,
        validate: Callable[[], Awaitable[bool]],
        on_success: Callable[[], Awaitable[None]],
        *,
        selective: bool,
    ) -> None:
        """Queue a guarded, bounded automatic read."""
        pass

    @abstractmethod
    async def send_reaction(self, chat_id: int, msg_id: int, emoji: str) -> bool:
        pass

    @abstractmethod
    async def send_automatic_reaction(
        self,
        chat_id: int,
        msg_id: int,
        emoji: str,
        validate: Callable[[], Awaitable[bool]],
        on_complete: Callable[[bool], Awaitable[None]],
    ) -> bool:
        pass


class TelegramAccountPort(ABC):
    @abstractmethod
    async def get_self_premium_status(self) -> bool:
        pass


class ChatRepository(
    ChatConnectionPort,
    ChatQueryPort,
    ForumQueryPort,
    TelegramMediaPort,
    TelegramEventPort,
    TelegramWritePort,
    TelegramAccountPort,
    ABC,
):
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
