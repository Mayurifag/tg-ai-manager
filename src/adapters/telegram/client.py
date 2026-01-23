import asyncio
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

from telethon import TelegramClient
from telethon.sessions import StringSession

from src.adapters.telegram.components.auth import TelegramAuthComponent
from src.adapters.telegram.components.listener import TelegramEventListener
from src.adapters.telegram.components.media import TelegramMediaComponent
from src.adapters.telegram.components.parser import TelegramMessageParser
from src.adapters.telegram.components.reader import TelegramChatReader
from src.adapters.telegram.components.writer import TelegramWriterComponent
from src.adapters.telegram.types import ITelethonClient
from src.domain.models import Chat, Message, SystemEvent
from src.domain.ports import ChatRepository
from src.infrastructure.event_bus import EventBus
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class TelethonAdapter(ChatRepository):
    def __init__(
        self,
        session_string: Optional[str],
        api_id: Optional[int],
        api_hash: Optional[str],
    ):
        self.session = StringSession(session_string or "")
        self.api_id = api_id
        self.api_hash = api_hash
        self.client: Optional[ITelethonClient] = None
        self._is_connected_flag = False

        # Initialize EventBus (Internal or External can be wired)
        # Here we initialize a local one, but ideally it should be injected.
        # For compatibility with container, we'll assign it later if needed,
        # but for components we need it now. We will update container to set it.
        self.event_bus = EventBus()

        if self.api_id and self.api_hash:
            self.client = TelegramClient(self.session, self.api_id, self.api_hash)  # type: ignore
        else:
            logger.info("adapter_initialized_no_credentials")

        cache_dir = os.path.join(os.getcwd(), "cache")

        # Initialize Components
        self.media = TelegramMediaComponent(self.client, cache_dir)
        self.parser = TelegramMessageParser(self.client, self.media)
        self.auth_comp = TelegramAuthComponent(self.client)
        self.reader = TelegramChatReader(self.client, self.media, self.parser)
        self.writer = TelegramWriterComponent(self.client, self.event_bus, self.reader)
        self.listener = TelegramEventListener(
            self.client, self.event_bus, self.parser, self.media, self.reader
        )

        # Legacy listeners support (bridged to EventBus)
        self.listeners: List[Callable[[SystemEvent], Awaitable[None]]] = []

    # --- Connection & Auth ---

    def is_connected(self) -> bool:
        return (
            self._is_connected_flag
            and self.client is not None
            and self.client.is_connected()
        )

    async def connect(self):
        if not self.client:
            return
        try:
            if not self.client.is_connected():
                await self.client.connect()

            if await self.client.is_user_authorized():
                self._is_connected_flag = True
                await self._fetch_self_id()
                self.listener.register_handlers()
            else:
                self._is_connected_flag = False
        except Exception as e:
            logger.error("connect_failed", error=str(e))
            self._is_connected_flag = False

    async def disconnect(self):
        self.auth_comp.stop()
        if self.client and self.client.is_connected():
            await self.client.disconnect()
        self._is_connected_flag = False

    async def _fetch_self_id(self):
        try:
            me = await self.client.get_me()
            if me:
                self.parser.set_self_id(me.id)
                logger.info("self_id_cached", user_id=me.id)
        except Exception:
            pass

    # --- Delegated Methods ---

    # Auth
    async def get_password_hint(self) -> str:
        return await self.auth_comp.get_password_hint()

    async def sign_in_with_password(self, password: str):
        await self.auth_comp.sign_in_with_password(password)
        self._is_connected_flag = True
        await self._fetch_self_id()
        self.listener.register_handlers()

    async def start_qr_login(self) -> str:
        url = await self.auth_comp.start_qr_login()
        if self.auth_comp.get_qr_status() == "authorized":
            self._is_connected_flag = True
            await self._fetch_self_id()
            self.listener.register_handlers()
        return url

    def get_qr_status(self) -> str:
        status = self.auth_comp.get_qr_status()
        if status == "authorized" and not self._is_connected_flag:
            # Late check if QR authorized in background
            self._is_connected_flag = True
            # Async init needs to happen in loop, but here we just return status
        return status

    def get_session_string(self) -> str:
        return self.session.save()

    # Chat Reader
    async def get_chats(self, limit: int) -> list[Chat]:
        return await self.reader.get_chats(limit)

    async def get_all_unread_chats(self) -> List[Chat]:
        return await self.reader.get_all_unread_chats()

    async def get_chat(self, chat_id: int) -> Optional[Chat]:
        return await self.reader.get_chat(chat_id)

    async def get_messages(
        self,
        chat_id: int,
        limit: int = 20,
        topic_id: Optional[int] = None,
        offset_id: int = 0,
        ids: Optional[List[int]] = None,
    ) -> List[Message]:
        return await self.reader.get_messages(chat_id, limit, topic_id, offset_id, ids)

    async def get_recent_authors(
        self, chat_id: int, limit: int = 100
    ) -> List[Dict[str, Any]]:
        return await self.reader.get_recent_authors(chat_id, limit)

    async def get_forum_topics(self, chat_id: int, limit: int = 20) -> List[Chat]:
        return await self.reader.get_forum_topics(chat_id, limit)

    async def get_unread_topics(self, chat_id: int) -> List[Chat]:
        return await self.reader.get_unread_topics(chat_id)

    async def get_topic_name(self, chat_id: int, topic_id: int) -> Optional[str]:
        return await self.reader.get_topic_name(chat_id, topic_id)

    # Writer
    async def mark_as_read(
        self, chat_id: int, topic_id: Optional[int] = None, max_id: Optional[int] = None
    ) -> None:
        await self.writer.mark_as_read(chat_id, topic_id, max_id)

    async def send_reaction(self, chat_id: int, msg_id: int, emoji: str) -> bool:
        return await self.writer.send_reaction(chat_id, msg_id, emoji)

    # Media
    async def download_media(
        self, chat_id: int, message_id: int, size_type: str = "preview"
    ) -> Optional[str]:
        return await self.media.download_message_media(chat_id, message_id, size_type)

    async def get_chat_avatar(self, chat_id: int) -> Optional[str]:
        return await self.media.download_avatar(chat_id)

    async def get_custom_emoji_media(self, document_id: int) -> Optional[str]:
        return await self.media.get_custom_emoji(document_id)

    async def run_storage_maintenance(self) -> None:
        limit_mb = int(os.getenv("CACHE_MAX_SIZE_MB", "1024"))
        limit_bytes = limit_mb * 1024 * 1024
        await asyncio.to_thread(self.media.run_maintenance_sync, limit_bytes)

    # Misc
    async def get_self_premium_status(self) -> bool:
        try:
            if not self.client:
                return False
            from telethon import functions, types

            users = await self.client(
                functions.users.GetUsersRequest(id=[types.InputUserSelf()])
            )
            if users:
                return getattr(users[0], "premium", False)
            return False
        except Exception:
            return False

    # Event Bridge
    def add_event_listener(self, callback: Callable[[SystemEvent], Awaitable[None]]):
        # Forward legacy listeners to EventBus
        self.event_bus.subscribe(callback)
