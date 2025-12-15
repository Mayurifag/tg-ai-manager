import os
from typing import Protocol, Any, Awaitable, List, Optional, Dict
from html import escape as html_escape
from telethon import TelegramClient, functions, utils
from telethon.extensions import html
from telethon.tl import types
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
            # download_big=False gets the small thumbnail
            result = await self.client.download_profile_photo(entity, file=path, download_big=False)
            if result:
                return f"/cache/{filename}"
        except Exception:
            pass
        return None

    def _get_sender_color(self, sender: Any, sender_id: int) -> str:
        """
        Determines the sender's color using Telegram's standard logic.
        Uses 'user.color' (PeerColor) if available, otherwise falls back to 'user_id % 7'.
        """
        color_index = 0

        # Check if the sender object has a specific 'color' attribute (PeerColor)
        # This covers Premium users or specific profile color settings
        peer_color = getattr(sender, 'color', None)
        if peer_color and hasattr(peer_color, 'color'):
             color_index = peer_color.color
        else:
             # Standard fallback: ID modulo 7
             color_index = abs(sender_id) % 7

        # Standard Telegram Palette (Red, Orange, Violet, Green, Cyan, Blue, Pink)
        # Using the standard hex codes found in Telegram Web K/A
        palette = [
            "#e17076", # 0: Red
            "#faa774", # 1: Orange
            "#a695e7", # 2: Violet
            "#7bc862", # 3: Green
            "#6ec9cb", # 4: Cyan
            "#65aadd", # 5: Blue
            "#ee7aae", # 6: Pink
        ]

        # Extended palette logic: If index > 6, it maps to one of the base colors modulo 7
        # (Though in reality Telegram has distinct colors for indices > 6, they are variations of these base ones)
        return palette[color_index % 7]

    def _get_sender_initials(self, name: str) -> str:
        if not name:
            return "?"
        parts = name.strip().split()
        if not parts:
            return "?"

        # First letter of first name
        first = parts[0][0]
        # First letter of last name (if exists)
        second = parts[1][0] if len(parts) > 1 else ""

        # If only one name part but it's long, take first two chars?
        # No, standard is usually just initials.
        # If single name "Telegram", "T". If "John Doe", "JD".
        return (first + second).upper()

    async def _fetch_forum_topics_response(self, peer: Any, limit: int) -> Optional[Any]:
        try:
            return await self.client(functions.messages.GetForumTopicsRequest(
                peer=peer, offset_date=None, offset_id=0, offset_topic=0, limit=limit, q=''
            ))
        except Exception as e:
            print(f"Failed to fetch topics: {e}")
            return None

    async def _get_top_messages_map(self, entity: Any, top_message_ids: List[int]) -> Dict[int, Any]:
        messages_map = {}
        if top_message_ids:
            try:
                msgs = await self.client.get_messages(entity, ids=top_message_ids)
                if msgs:
                    for m in msgs:
                        if m: messages_map[m.id] = m
            except Exception as e:
                print(f"Warning: Failed to fetch top messages: {e}")
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

    async def get_chat(self, chat_id: int) -> Optional[Chat]:
        try:
            entity = await self.client.get_entity(chat_id)
            name = utils.get_display_name(entity)
            c_type = ChatType.GROUP
            if isinstance(entity, types.User):
                c_type = ChatType.USER
            elif isinstance(entity, types.Chat):
                c_type = ChatType.GROUP
            elif isinstance(entity, types.Channel):
                if getattr(entity, 'forum', False): c_type = ChatType.FORUM
                elif getattr(entity, 'broadcast', False): c_type = ChatType.CHANNEL
                else: c_type = ChatType.GROUP

            image_url = await self._get_chat_image(entity, chat_id)
            return Chat(id=chat_id, name=name, unread_count=0, type=c_type, image_url=image_url)
        except Exception as e:
            print(f"Error fetching chat info {chat_id}: {e}")
            return None

    async def get_messages(self, chat_id: int, limit: int = 20, topic_id: Optional[int] = None) -> List[Message]:
        try:
            entity = await self.client.get_entity(chat_id)
            messages = await self.client.get_messages(entity, limit=limit, reply_to=topic_id)

            result_messages = []
            for msg in messages:
                raw_text = getattr(msg, 'message', '') or ""
                entities = getattr(msg, 'entities', [])
                text = ""
                if raw_text:
                    try:
                        text = html.unparse(raw_text, entities or [])
                    except Exception:
                        text = html_escape(raw_text)
                if not text: text = "[Media/Sticker]"

                sender_name = "Unknown"
                sender_id = 0
                avatar_url = None
                sender_color = None
                sender_initials = "?"

                # Try to get sender from the message object
                sender = getattr(msg, 'sender', None)

                # If sender is missing (common in groups where users aren't cached), try to fetch explicitly
                if not sender and getattr(msg, 'from_id', None):
                    try:
                        sender = await msg.get_sender()
                    except Exception:
                        pass

                # Resolve ID from sender or from_id
                if sender:
                    sender_id = sender.id
                    sender_name = utils.get_display_name(sender)
                    avatar_url = await self._get_chat_image(sender, sender_id)
                elif getattr(msg, 'from_id', None):
                     if isinstance(msg.from_id, types.PeerUser):
                         sender_id = msg.from_id.user_id
                     elif isinstance(msg.from_id, types.PeerChannel):
                         sender_id = msg.from_id.channel_id

                # Get the correct color using the sender object (for PeerColor support) or ID
                sender_color = self._get_sender_color(sender, sender_id)
                sender_initials = self._get_sender_initials(sender_name)

                result_messages.append(Message(
                    id=msg.id,
                    text=text,
                    date=msg.date,
                    sender_name=sender_name,
                    is_outgoing=getattr(msg, 'out', False),
                    sender_id=sender_id,
                    avatar_url=avatar_url,
                    sender_color=sender_color,
                    sender_initials=sender_initials
                ))
            return result_messages
        except Exception as e:
            print(f"Error fetching messages: {e}")
            return []

    async def get_forum_topics(self, chat_id: int, limit: int = 20) -> List[Chat]:
        try:
            entity = await self.client.get_entity(chat_id)
            response = await self._fetch_forum_topics_response(entity, limit=limit)
            topics = []
            if not response: return topics

            top_message_ids = [t.top_message for t in response.topics]
            messages_map = await self._get_top_messages_map(entity, top_message_ids)

            for t in response.topics:
                last_msg = messages_map.get(t.top_message)
                preview = format_message_preview(last_msg, ChatType.TOPIC)
                topics.append(Chat(
                    id=t.id, name=t.title, unread_count=t.unread_count,
                    type=ChatType.TOPIC, last_message_preview=preview,
                    icon_emoji=getattr(t, 'icon_emoji', None)
                ))
            return topics
        except Exception as e:
            print(f"Error topics: {e}")
            return []
