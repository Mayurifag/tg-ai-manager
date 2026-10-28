from unittest.mock import AsyncMock, Mock, patch

from telethon import TelegramClient
from telethon.sessions import StringSession

from src.adapters.telegram.client import TelethonAdapter
from src.adapters.telegram.recovering_client import RecoveringTelegramClient


def make_client():
    return RecoveringTelegramClient(StringSession(), 12345, "test-api-hash")


async def test_auto_reconnect_clears_stale_ping_and_requests_catchup():
    client = make_client()
    client._sender._ping = 123
    client._sender.send = Mock()
    client._sender._start_reconnect = Mock()
    client.catch_up = AsyncMock()

    with patch.object(TelegramClient, "_handle_auto_reconnect", new_callable=AsyncMock):
        await client._handle_auto_reconnect()

    assert client.reconnect_count == 1
    client.catch_up.assert_awaited_once()
    # Before the fix this sees the old ping and starts another reconnect.
    client._sender._keepalive_ping(456)
    assert client._sender._ping == 456
    client._sender.send.assert_called_once()
    client._sender._start_reconnect.assert_not_called()


async def test_manual_reconnect_clears_ping_from_previous_connection():
    client = make_client()
    client._sender._ping = 123
    with patch.object(TelegramClient, "connect", new_callable=AsyncMock):
        await client.connect()
    assert client._sender._ping is None


async def test_connect_does_not_hide_pending_ping_on_live_connection():
    client = make_client()
    client._sender._user_connected = True
    client._sender._ping = 123
    with patch.object(TelegramClient, "connect", new_callable=AsyncMock):
        await client.connect()
    assert client._sender._ping == 123


def test_adapter_readiness_requires_handlers_and_write_worker():
    adapter = object.__new__(TelethonAdapter)
    adapter.client = Mock()
    adapter.client.is_connected.return_value = True
    adapter._is_connected_flag = True
    adapter._event_handler_registered = True
    adapter._write_queue = Mock()
    adapter._write_queue.is_running.return_value = True
    assert adapter.is_connected()
    adapter._write_queue.is_running.return_value = False
    assert not adapter.is_connected()
    adapter._write_queue.is_running.return_value = True
    adapter._event_handler_registered = False
    assert not adapter.is_connected()
