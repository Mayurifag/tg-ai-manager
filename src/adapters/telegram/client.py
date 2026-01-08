import os
import asyncio
import traceback
from typing import List, Callable, Awaitable, Dict, Optional
from telethon import TelegramClient, events, functions
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
    SendCodeUnavailableError
)
from telethon.tl.functions.account import GetPasswordRequest
from src.domain.ports import ChatRepository
from src.domain.models import SystemEvent
from src.adapters.telegram.types import ITelethonClient
from src.adapters.telegram.media import MediaMixin
from src.adapters.telegram.message_parser import MessageParserMixin
from src.adapters.telegram.chat_operations import ChatOperationsMixin
from src.adapters.telegram.event_handlers import EventHandlersMixin
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class TelethonAdapter(
    MediaMixin,
    MessageParserMixin,
    ChatOperationsMixin,
    EventHandlersMixin,
    ChatRepository,
):
    def __init__(self, session_string: Optional[str], api_id: Optional[int], api_hash: Optional[str]):
        self.session = StringSession(session_string or "")

        self.api_id = api_id
        self.api_hash = api_hash
        self.client: Optional[ITelethonClient] = None

        if self.api_id and self.api_hash:
            self.client = TelegramClient(
                self.session, self.api_id, self.api_hash
            )  # type: ignore
        else:
            logger.info("adapter_initialized_no_credentials")

        self.images_dir = os.path.join(os.getcwd(), "cache")
        self.listeners: List[Callable[[SystemEvent], Awaitable[None]]] = []
        self._event_handler_registered = False
        self._msg_id_to_chat_id: Dict[int, int] = {}
        self._is_connected_flag = False

        # QR Login State
        self._qr_login = None
        self._qr_task = None
        self._qr_status = "none"  # none, waiting, authorized, needs_password, expired, error

        os.makedirs(self.images_dir, exist_ok=True)
        self.cleanup_startup_cache()

    def is_connected(self) -> bool:
        return self._is_connected_flag and self.client is not None and self.client.is_connected()

    async def connect(self):
        if not self.client:
            logger.warning("connect_skipped_no_client_initialized")
            return

        try:
            if not self.client.is_connected():
                await self.client.connect()

            if await self.client.is_user_authorized():
                self._is_connected_flag = True
                self._register_handlers()
            else:
                self._is_connected_flag = False
                logger.warning("client_connected_but_unauthorized")
        except Exception as e:
            logger.error("connect_failed", error=str(e))
            self._is_connected_flag = False

    def _register_handlers(self):
        if self.client and not self._event_handler_registered:
            self.client.add_event_handler(self._handle_new_message, events.NewMessage())
            self.client.add_event_handler(self._handle_edited_message, events.MessageEdited())
            self.client.add_event_handler(self._handle_deleted_message, events.MessageDeleted())
            self.client.add_event_handler(self._handle_chat_action, events.ChatAction())
            self._event_handler_registered = True

    async def disconnect(self):
        if self._qr_task:
            self._qr_task.cancel()
            self._qr_task = None
        if self.client and self.client.is_connected():
            await self.client.disconnect()
        self._is_connected_flag = False

    # --- Auth Methods ---

    async def send_code(self, phone: str, force_sms: bool = False):
        if not self.client:
            raise RuntimeError("Client not initialized (missing api_id/hash)")

        if not phone.startswith("+"):
            phone = f"+{phone}"

        logger.info("send_code_connecting", phone=phone, force_sms=force_sms)
        try:
            if not self.client.is_connected():
                await self.client.connect()

            logger.info("send_code_requesting", phone=phone)
            result = await self.client.send_code_request(phone, force_sms=force_sms)
            logger.info("send_code_success", result=str(result))
            return result
        except FloodWaitError as e:
            logger.error("send_code_flood_wait", seconds=e.seconds)
            raise ValueError(f"Too many requests. Please wait {e.seconds} seconds.")
        except Exception as e:
            logger.error(
                "send_code_error",
                error=str(e),
                traceback=traceback.format_exc()
            )
            raise e

    async def resend_code(self, phone: str, phone_code_hash: str):
        """
        Triggers the next delivery method using raw API request.
        """
        if not self.client:
            raise RuntimeError("Client not initialized")

        if not phone.startswith("+"):
            phone = f"+{phone}"

        try:
            if not self.client.is_connected():
                await self.client.connect()

            logger.info("resend_code_requesting", phone=phone)

            # Fix: Use raw MTProto request to ensure it triggers correctly
            result = await self.client(functions.auth.ResendCodeRequest(
                phone_number=phone,
                phone_code_hash=phone_code_hash
            ))

            logger.info("resend_code_success", result=str(result))
            return result
        except SendCodeUnavailableError:
            logger.error("resend_code_unavailable", phone=phone)
            raise ValueError("No further code delivery methods are available.")
        except FloodWaitError as e:
            logger.error("resend_code_flood_wait", seconds=e.seconds)
            raise ValueError(f"Too many requests. Please wait {e.seconds} seconds.")
        except Exception as e:
            logger.error("resend_code_failed", error=str(e), traceback=traceback.format_exc())
            raise e

    async def sign_in(self, phone: str, code: str, phone_code_hash: str):
        if not self.client:
            raise RuntimeError("Client not initialized")

        if not phone.startswith("+"):
            phone = f"+{phone}"

        try:
            logger.info("sign_in_attempt", phone=phone)
            await self.client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            self._is_connected_flag = True
            self._register_handlers()
            logger.info("sign_in_success", phone=phone)
            return "logged_in"
        except SessionPasswordNeededError:
            logger.info("sign_in_2fa_required", phone=phone)
            return "needs_password"
        except PhoneCodeInvalidError:
            logger.info("sign_in_invalid_code", phone=phone)
            raise ValueError("Invalid code entered.")
        except PhoneCodeExpiredError:
            logger.info("sign_in_expired_code", phone=phone)
            raise ValueError("The code has expired. Please request a new one.")
        except Exception as e:
            logger.error("sign_in_failed", error=str(e), traceback=traceback.format_exc())
            raise e

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
        try:
            logger.info("qr_wait_loop_started")
            await self._qr_login.wait()
            self._qr_status = "authorized"
            self._is_connected_flag = True
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
