import os
from typing import Protocol, Any, Awaitable, List, Optional, Dict
from telethon import TelegramClient, functions, utils
from src.domain.ports import ChatRepository
from src.domain.models import Chat, ChatType, Message
from src.adapters.telethon_mappers import map_telethon_dialog_to_chat_type, format_message_preview

class ITelethonClient(Protocol):
    def connect(self) -> Awaitable[None]: ...
    def disconnect(self) -> Awaitable[None]: ...
    def is_user_authorized(self) -> Awaitable[bool]: ...
    def start(self) -> Awaitable[Any]: ...
    def get_dialogs(self, limit: int) -> Awaitable[Any]: ...
    def get_messages(self, entity: Any, limit: int = 20, reply_to: int = None, ids: List[int] = None) -> Awaitable[Any]: ...
    def get_entity(self, entity: Any) -> Awaitable[Any]: ...
    def download_profile_photo(self, entity: Any, file: Any = None, download_big: bool = False) -> Awaitable[Any]: ...
    def __call__(self, request: Any) -> Awaitable[Any]: ...

class TelethonAdapter(ChatRepository):
    def __init__(self, session_name: str, api_id: int, api_hash: str):
        self.client: ITelethonClient = TelegramClient(session_name, api_id, api_hash) # type: ignore
        self.images_dir = os.path.join(os.getcwd(), "cache")

    async def connect(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.start()

    async def disconnect(self):
        await self.client.disconnect()

    async def _get_chat_image(self, entity: Any, chat_id: int) -> Optional[str]:
        filename = f"{chat_id}.jpg"
        path = os.path.join(self.images_dir, filename)

        if os.path.exists(path):
            return f"/cache/{filename}"

        try:
            result = await self.client.download_profile_photo(entity, file=path)
            if result:
                return f"/cache/{filename}"
        except Exception:
            pass
        return None

    async def _fetch_forum_topics_response(self, peer: Any, limit: int) -> Optional[Any]:
        """Fetches forum topics response and handles common exceptions."""
        try:
            return await self.client(functions.messages.GetForumTopicsRequest(
                peer=peer,
                offset_date=None,
                offset_id=0,
                offset_topic=0,
                limit=limit,
                q=''
            ))
        except Exception as e:
            # Use the peer ID or a representation for logging
            peer_id_str = str(utils.get_peer_id(peer)) if hasattr(utils, 'get_peer_id') else str(peer)
            print(f"Failed to fetch topics for forum ID {peer_id_str}: {e.__class__.__name__}: {e}")
            return None

    async def _get_top_messages_map(self, entity: Any, top_message_ids: List[int]) -> Dict[int, Any]:
        """Fetches top messages for topics and returns a map from message id to message object."""
        messages_map = {}
        if top_message_ids:
            try:
                # Use a specific limit for this call if required, but default get_messages is fine
                msgs = await self.client.get_messages(entity, ids=top_message_ids)
                if msgs:
                    for m in msgs:
                        if m:
                            messages_map[m.id] = m
            except Exception as e:
                # Log a softer error if fetching top messages fails, as it might be non-critical
                print(f"Warning: Failed to fetch some top messages: {e}")
        return messages_map

    async def get_chats(self, limit: int) -> list[Chat]:
        dialogs = await self.client.get_dialogs(limit=limit)
        results = []
        for d in dialogs:
            chat_type = map_telethon_dialog_to_chat_type(d)

            unread_topics_count = None
            calculated_unread_count = d.unread_count
            topic_map = {}

            if chat_type == ChatType.FORUM:
                # Use the new helper for fetching topics
                response = await self._fetch_forum_topics_response(d.entity, limit=20)

                if response:
                    active_topics = response.topics
                    for t in active_topics:
                        topic_map[t.id] = t.title

                    unread_topics = [t for t in active_topics if t.unread_count > 0]
                    if unread_topics:
                        unread_topics_count = len(unread_topics)
                        calculated_unread_count = sum(t.unread_count for t in unread_topics)

            msg = getattr(d, 'message', None)

            preview = format_message_preview(msg, chat_type, topic_map)

            image_url = await self._get_chat_image(d.entity, d.id)

            results.append(Chat(
                id=d.id,
                name=d.name,
                unread_count=calculated_unread_count,
                type=chat_type,
                unread_topics_count=unread_topics_count,
                last_message_preview=preview,
                image_url=image_url
            ))
        return results

    async def get_messages(self, chat_id: int, limit: int = 20, topic_id: Optional[int] = None) -> List[Message]:
        try:
            entity = await self.client.get_entity(chat_id)
            messages = await self.client.get_messages(entity, limit=limit, reply_to=topic_id)

            result_messages = []
            for msg in messages:
                text = getattr(msg, 'message', '')
                if not text:
                     text = "[Media/Sticker]"

                sender_name = "Unknown"
                sender = getattr(msg, 'sender', None)
                if sender:
                     sender_name = utils.get_display_name(sender)

                result_messages.append(Message(
                    id=msg.id,
                    text=text,
                    date=msg.date,
                    sender_name=sender_name,
                    is_outgoing=getattr(msg, 'out', False)
                ))
            return result_messages
        except Exception as e:
            print(f"Error fetching messages: {e}")
            return []

    async def get_forum_topics(self, chat_id: int, limit: int = 20) -> List[Chat]:
        try:
            entity = await self.client.get_entity(chat_id)
            # Use the new helper for fetching topics response
            response = await self._fetch_forum_topics_response(entity, limit=limit)

            topics = []
            if not response:
                return topics # Return empty list if fetching failed

            top_message_ids = [t.top_message for t in response.topics]

            # Use the new helper to fetch top messages
            messages_map = await self._get_top_messages_map(entity, top_message_ids)

            for t in response.topics:
                last_msg = messages_map.get(t.top_message)

                preview = format_message_preview(last_msg, ChatType.TOPIC)

                icon_emoji = getattr(t, 'icon_emoji', None)

                topics.append(Chat(
                    id=t.id,
                    name=t.title,
                    unread_count=t.unread_count,
                    type=ChatType.TOPIC,
                    last_message_preview=preview,
                    image_url=None,
                    icon_emoji=icon_emoji
                ))

            return topics
        except Exception as e:
            # Only need to catch exceptions that might occur after the _fetch_forum_topics_response call
            print(f"Error post-processing forum topics for chat {chat_id}: {e}")
            return []
