import asyncio

from telethon import TelegramClient, utils
from telethon.extensions.messagepacker import MessagePacker

from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class _WriteAwarePacker(MessagePacker):
    def extend(self, states):
        for state in states:
            if not getattr(state.request, "_send_once", False):
                self.append(state)
            elif not state.future.done():
                state.future.set_exception(
                    ConnectionError("Telegram write interrupted")
                )

    def discard(self, future):
        for state in self._deque:
            if state.future is future:
                self._deque.remove(state)
                break


class RecoveringTelegramClient(TelegramClient):
    """Restore live updates after a transport interruption.

    Telethon 1.x keeps its pending keepalive ping across connections. If its
    pong was lost, every subsequent keepalive reconnects again. Reset that
    connection-local flag only at a connection boundary. Keep this workaround
    isolated and verify it when upgrading Telethon until upstream fixes it.
    """

    reconnect_count = 0

    async def send_once(self, request):
        # See WORKAROUNDS-ADR.md: the regular RPC path and sender both retry.
        sender = self._sender
        if not isinstance(sender._send_queue, _WriteAwarePacker):
            raise ConnectionError("Telegram write transport unavailable")
        await request.resolve(self, utils)
        request._send_once = True
        future = sender.send(request)
        try:
            async with asyncio.timeout(15):
                return await future
        except asyncio.CancelledError as error:
            if asyncio.current_task().cancelling():
                raise
            # The sender cancels request futures on disconnect without
            # canceling the queue worker itself.
            raise ConnectionError("Telegram write interrupted") from error
        finally:
            if future.cancelled():
                sender._send_queue.discard(future)

    async def connect(self) -> None:
        if not self.is_connected():
            self._sender._ping = None
            if not isinstance(self._sender._send_queue, _WriteAwarePacker):
                self._sender._send_queue = _WriteAwarePacker(
                    self._sender._state, self._sender._loggers
                )
        await super().connect()

    async def _handle_auto_reconnect(self) -> None:
        self._sender._ping = None
        self.reconnect_count += 1
        logger.warning("telegram_reconnected", reconnect_count=self.reconnect_count)
        await super()._handle_auto_reconnect()
        # Telethon's default callback returns before its catch_up() code.
        # Schedule getDifference using the update state retained in memory.
        await self.catch_up()
