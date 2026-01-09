import asyncio
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

from telethon import TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetPasswordRequest

from src.adapters.telegram.chat_operations import ChatOperationsMixin
from src.adapters.telegram.event_handlers import EventHandlersMixin
from src.adapters.telegram.media import MediaMixin
from src.adapters.telegram.message_parser import MessageParserMixin
from src.adapters.telegram.types import ITelethonClient
from src.domain.models import SystemEvent
from src.domain.ports import ChatRepository
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class TelethonAdapter(
    MediaMixin,
    MessageParserMixin,
    EventHandlersMixin,
    ChatOperationsMixin,
    ChatRepository,
):
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

        # ID Cache for reaction checks
        self._self_id: Optional[int] = None

        if self.api_id and self.api_hash:
            self.client = TelegramClient(self.session, self.api_id, self.api_hash)  # type: ignore
        else:
            logger.info("adapter_initialized_no_credentials")

        self.images_dir = os.path.join(os.getcwd(), "cache")
        self.listeners: List[Callable[[SystemEvent], Awaitable[None]]] = []
        self._event_handler_registered = False
        self._msg_id_to_chat_id: Dict[int, int] = {}
        self._is_connected_flag = False

        # QR Login State
        self._qr_login: Any = None
        self._qr_task: Optional[asyncio.Task] = None
        self._qr_status = (
            "none"  # none, waiting, authorized, needs_password, expired, error
        )

        os.makedirs(self.images_dir, exist_ok=True)
        self.cleanup_startup_cache()

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
                    logger.info("self_id_cached", user_id=self._self_id)
        except Exception as e:
            logger.error("fetch_self_id_failed", error=str(e))

    def _register_handlers(self):
        if self.client and not self._event_handler_registered:
            self.client.add_event_handler(self._handle_new_message, events.NewMessage())
            self.client.add_event_handler(
                self._handle_edited_message, events.MessageEdited()
            )
            self.client.add_event_handler(
                self._handle_deleted_message, events.MessageDeleted()
            )
            self.client.add_event_handler(self._handle_chat_action, events.ChatAction())
            # Handle Raw updates (e.g. reactions)
            self.client.add_event_handler(self._handle_raw_updates)
            self._event_handler_registered = True

    async def disconnect(self):
        if self._qr_task:
            self._qr_task.cancel()
            self._qr_task = None
        if self.client and self.client.is_connected():
            await self.client.disconnect()
        self._is_connected_flag = False

    # --- Auth Methods ---

    async def get_password_hint(self) -> str:
        if not self.client:
            return ""
        try:
            # We assume client is connected if we reached 2FA stage
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

        # Check if already authorized
        if await self.client.is_user_authorized():
            self._qr_status = "authorized"
            return "authorized"

        # Cancel existing task if any
        if self._qr_task:
            self._qr_task.cancel()

        self._qr_login = await self.client.qr_login()
        self._qr_status = "waiting"

        # Start background wait
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
