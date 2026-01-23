import asyncio
from typing import Any, Optional

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class TelegramAuthComponent:
    def __init__(self, client: TelegramClient):
        self.client = client
        self._qr_login: Any = None
        self._qr_task: Optional[asyncio.Task] = None
        self._qr_status = "none"

    async def get_password_hint(self) -> str:
        from telethon.tl.functions.account import GetPasswordRequest

        try:
            pwd_info = await self.client(GetPasswordRequest())
            return pwd_info.hint if pwd_info and pwd_info.hint else "No hint provided"
        except Exception as e:
            logger.error("get_password_hint_failed", error=str(e))
            return ""

    async def sign_in_with_password(self, password: str):
        try:
            await self.client.sign_in(password=password)
        except Exception as e:
            logger.error("2fa_failed", error=str(e))
            raise e

    async def start_qr_login(self) -> str:
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
            return
        try:
            await self._qr_login.wait()
            self._qr_status = "authorized"
        except SessionPasswordNeededError:
            self._qr_status = "needs_password"
        except asyncio.TimeoutError:
            self._qr_status = "expired"
        except Exception as e:
            self._qr_status = "error"
            logger.error("qr_login_error", error=str(e))

    def get_qr_status(self) -> str:
        return self._qr_status

    def stop(self):
        if self._qr_task:
            self._qr_task.cancel()
