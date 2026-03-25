import asyncio
import os
from typing import Any, Awaitable, Callable, List, Optional

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetPasswordRequest

from src.adapters.telegram.chat_operations import ChatOps
from src.adapters.telegram.event_handlers import EventHandlers
from src.adapters.telegram.media import MediaManager
from src.adapters.telegram.message_parser import MessageParser
from src.adapters.telegram.types import ITelethonClient
from src.config import get_settings
from src.domain.models import SystemEvent
from src.domain.ports import ChatRepository
from src.infrastructure.logging import get_logger
from src.infrastructure.telegram_queue import TelegramWriteQueue

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

        self._self_id: Optional[int] = None

        if self.api_id and self.api_hash:
            self.client = TelegramClient(self.session, self.api_id, self.api_hash)  # type: ignore
        else:
            logger.info("adapter_initialized_no_credentials")

        self.images_dir = os.path.join(os.getcwd(), "cache")
        self._event_handler_registered = False
        self._is_connected_flag = False

        # QR Login State
        self._qr_login: Any = None
        self._qr_task: Optional[asyncio.Task] = None
        self._qr_status = (
            "none"  # none, waiting, authorized, needs_password, expired, error
        )

        # Write queue
        self._write_queue = TelegramWriteQueue(delay=get_settings().WRITE_QUEUE_DELAY)

        os.makedirs(self.images_dir, exist_ok=True)

        # Build collaborators
        self._media = MediaManager(self.client, self.images_dir)
        self._parser = MessageParser(self.client, self._media)
        self._chat_ops = ChatOps(
            client=self.client,
            parser=self._parser,
            media=self._media,
            write_queue=self._write_queue,
            dispatch_fn=None,  # patched below after EventHandlers is built
        )
        self._event_handlers = EventHandlers(
            client=self.client,
            parser=self._parser,
            media=self._media,
            get_topic_name_fn=self._chat_ops.get_topic_name,
        )
        # Resolve circular dep: ChatOps.dispatch_fn → EventHandlers._dispatch
        self._chat_ops._dispatch_fn = self._event_handlers._dispatch

        self._media.cleanup_startup_cache()

    def is_connected(self) -> bool:
        return (
            self._is_connected_flag
            and self.client is not None
            and self.client.is_connected()
        )

    async def connect(self):
        if not self.client:
            logger.warning("connect_skipped_no_client_initialized")
            return

        try:
            if not self.client.is_connected():
                await self.client.connect()

            if await self.client.is_user_authorized():
                self._is_connected_flag = True
                await self._write_queue.start()
                await self._fetch_self_id()
                self._register_handlers()
            else:
                self._is_connected_flag = False
                logger.warning("client_connected_but_unauthorized")
        except Exception as e:
            logger.error("connect_failed", error=str(e))
            self._is_connected_flag = False

    async def _fetch_self_id(self):
        """Cache the authenticated user ID for reaction parsing."""
        try:
            if self.client:
                me = await self.client.get_me()
                if me:
                    self._self_id = me.id
                    self._parser.self_id = me.id
                    logger.info("self_id_cached", user_id=self._self_id)
        except Exception as e:
            logger.error("fetch_self_id_failed", error=str(e))

    def _register_handlers(self):
        if self.client and not self._event_handler_registered:
            self._event_handlers.register_handlers(self.client)
            self._event_handler_registered = True

    async def disconnect(self):
        if self._qr_task:
            self._qr_task.cancel()
            self._qr_task = None
        await self._write_queue.stop()
        if self.client and self.client.is_connected():
            await self.client.disconnect()
        self._is_connected_flag = False

    # --- Auth Methods ---

    async def get_password_hint(self) -> str:
        if not self.client:
            return ""
        try:
            pwd_info = await self.client(GetPasswordRequest())
            return pwd_info.hint if pwd_info and pwd_info.hint else "No hint provided"
        except Exception as e:
            logger.error("get_password_hint_failed", error=str(e))
            return ""

    async def sign_in_with_password(self, password: str):
        if not self.client:
            raise RuntimeError("Client not initialized")

        try:
            logger.info("2fa_attempt")
            await self.client.sign_in(password=password)
            self._is_connected_flag = True
            await self._write_queue.start()
            await self._fetch_self_id()
            self._register_handlers()
            logger.info("2fa_success")
        except Exception as e:
            logger.error("2fa_failed", error=str(e))
            raise e

    # --- QR Code Logic ---

    async def start_qr_login(self) -> str:
        if not self.client:
            raise RuntimeError("Client not initialized")

        if not self.client.is_connected():
            await self.client.connect()

        if await self.client.is_user_authorized():
            self._qr_status = "authorized"
            return "authorized"

        if self._qr_task:
            self._qr_task.cancel()

        self._qr_login = await self.client.qr_login()
        self._qr_status = "waiting"

        self._qr_task = asyncio.create_task(self._qr_wait_loop())

        return self._qr_login.url

    async def _qr_wait_loop(self):
        if not self._qr_login:
            logger.error("qr_login_object_missing")
            self._qr_status = "error"
            return

        try:
            logger.info("qr_wait_loop_started")
            await self._qr_login.wait()
            self._qr_status = "authorized"
            self._is_connected_flag = True
            await self._write_queue.start()
            await self._fetch_self_id()
            self._register_handlers()
            logger.info("qr_login_success")
        except SessionPasswordNeededError:
            self._qr_status = "needs_password"
            logger.info("qr_login_needs_password")
        except asyncio.TimeoutError:
            self._qr_status = "expired"
            logger.info("qr_login_expired")
        except Exception as e:
            self._qr_status = "error"
            logger.error("qr_login_error", error=str(e))

    def get_qr_status(self) -> str:
        return self._qr_status

    def get_session_string(self) -> str:
        return self.session.save()

    # --- ChatRepository delegation ---

    async def get_chats(self, limit: int):
        return await self._chat_ops.get_chats(limit)

    async def get_all_unread_chats(self):
        return await self._chat_ops.get_all_unread_chats()

    async def get_chat(self, chat_id: int):
        return await self._chat_ops.get_chat(chat_id)

    async def get_messages(
        self,
        chat_id: int,
        limit: int = 20,
        topic_id: Optional[int] = None,
        offset_id: int = 0,
        ids: Optional[List[int]] = None,
    ):
        return await self._chat_ops.get_messages(
            chat_id, limit=limit, topic_id=topic_id, offset_id=offset_id, ids=ids
        )

    async def get_recent_authors(self, chat_id: int, limit: int = 100):
        return await self._chat_ops.get_recent_authors(chat_id, limit)

    async def get_forum_topics(self, chat_id: int, limit: int = 20):
        return await self._chat_ops.get_forum_topics(chat_id, limit)

    async def get_unread_topics(self, chat_id: int):
        return await self._chat_ops.get_unread_topics(chat_id)

    async def get_topic_name(self, chat_id: int, topic_id: int):
        return await self._chat_ops.get_topic_name(chat_id, topic_id)

    async def mark_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int] = None,
        max_id: Optional[int] = None,
    ) -> None:
        return await self._chat_ops.mark_as_read(chat_id, topic_id, max_id)

    async def send_reaction(self, chat_id: int, msg_id: int, emoji: str) -> bool:
        return await self._chat_ops.send_reaction(chat_id, msg_id, emoji)

    async def get_self_premium_status(self) -> bool:
        return await self._chat_ops.get_self_premium_status()

    async def download_media(
        self, chat_id: int, message_id: int, size_type: str = "preview"
    ) -> Optional[str]:
        return await self._media.download_media(chat_id, message_id, size_type)

    async def get_chat_avatar(self, chat_id: int) -> Optional[str]:
        return await self._media.get_chat_avatar(chat_id)

    async def run_storage_maintenance(self) -> None:
        return await self._media.run_storage_maintenance()

    def add_event_listener(
        self, callback: Callable[[SystemEvent], Awaitable[None]]
    ) -> None:
        self._event_handlers.add_event_listener(callback)

    # --- Additional methods used by routes ---

    async def get_custom_emoji_media(self, doc_id: int) -> Optional[str]:
        return await self._media.get_custom_emoji_media(doc_id)
