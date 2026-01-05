import os
from typing import List, Callable, Awaitable, Dict
from telethon import TelegramClient, events
from src.domain.ports import ChatRepository
from src.domain.models import SystemEvent
from src.adapters.telegram.types import ITelethonClient
from src.adapters.telegram.media import MediaMixin
from src.adapters.telegram.message_parser import MessageParserMixin
from src.adapters.telegram.chat_operations import ChatOperationsMixin
from src.adapters.telegram.event_handlers import EventHandlersMixin

class TelethonAdapter(
    MediaMixin,           # Provides: _get_chat_image, download_media, cleanup_startup_cache
    MessageParserMixin,   # Provides: _parse_message, _cache_message_chat. Consumes: _get_chat_image
    ChatOperationsMixin,  # Provides: get_chats, get_topic_name. Consumes: _parse_message
    EventHandlersMixin,   # Provides: add_event_listener. Consumes: get_topic_name
    ChatRepository        # Abstract Base Class (Interface)
):
    def __init__(self, session_name: str, api_id: int, api_hash: str):
        self.client: ITelethonClient = TelegramClient(session_name, api_id, api_hash) # type: ignore
        self.images_dir = os.path.join(os.getcwd(), "cache")
        self.listeners: List[Callable[[SystemEvent], Awaitable[None]]] = []
        self._event_handler_registered = False
        self._msg_id_to_chat_id: Dict[int, int] = {}

        # Ensure cache directory exists
        os.makedirs(self.images_dir, exist_ok=True)

        # Cleanup old avatars on startup
        self.cleanup_startup_cache()

    async def connect(self):
        if not self.client.is_connected():
            await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.start()

        if not self._event_handler_registered:
            self.client.add_event_handler(self._handle_new_message, events.NewMessage())
            self.client.add_event_handler(self._handle_edited_message, events.MessageEdited())
            self.client.add_event_handler(self._handle_deleted_message, events.MessageDeleted())
            self.client.add_event_handler(self._handle_chat_action, events.ChatAction())
            self._event_handler_registered = True

    async def disconnect(self):
        if self.client.is_connected():
            await self.client.disconnect()
