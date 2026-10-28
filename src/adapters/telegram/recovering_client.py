from telethon import TelegramClient

from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class RecoveringTelegramClient(TelegramClient):
    """Restore live updates after a transport interruption.

    Telethon 1.x keeps its pending keepalive ping across connections. If its
    pong was lost, every subsequent keepalive reconnects again. Reset that
    connection-local flag only at a connection boundary. Keep this workaround
    isolated and covered by regression tests until upstream fixes it.
    """

    reconnect_count = 0

    async def connect(self) -> None:
        if not self.is_connected():
            self._sender._ping = None
        await super().connect()

    async def _handle_auto_reconnect(self) -> None:
        self._sender._ping = None
        self.reconnect_count += 1
        logger.warning("telegram_reconnected", reconnect_count=self.reconnect_count)
        await super()._handle_auto_reconnect()
        # Telethon's default callback returns before its catch_up() code.
        # Schedule getDifference using the update state retained in memory.
        await self.catch_up()
