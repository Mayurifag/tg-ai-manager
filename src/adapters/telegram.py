import os
from typing import Protocol, Any, Awaitable, List, Optional
from telethon import TelegramClient, functions, utils
from telethon.tl.types import Message as TlMessage, MessageService
from src.domain.ports import ChatRepository
from src.domain.models import Chat, ChatType, Message

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
        self.images_dir = os.path.join(os.getcwd(), "static", "images")

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
            return f"/static/images/{filename}"

        try:
            result = await self.client.download_profile_photo(entity, file=path)
            if result:
                return f"/static/images/{filename}"
        except Exception:
            pass
        return None

    def _format_preview(self, message: Any, chat_type: ChatType, topic_name: Optional[str] = None) -> str:
        if not message:
            return "No messages"

        if isinstance(message, MessageService):
            return "Service message"

        text = getattr(message, 'message', '')
        if not text:
            return "Media/Sticker"

        text = text.replace('\n', ' ')
        if len(text) > 50:
            text = text[:47] + "..."

        prefix = ""
        if chat_type == ChatType.GROUP:
            sender = getattr(message, 'sender', None)
            if sender:
                name = utils.get_display_name(sender)
                prefix = f"{name}: "
        elif chat_type == ChatType.FORUM and topic_name:
            prefix = f"{topic_name}: "

        return f"{prefix}{text}"

    async def get_chats(self, limit: int) -> list[Chat]:
        dialogs = await self.client.get_dialogs(limit=limit)
        results = []
        for d in dialogs:
            chat_type = ChatType.GROUP

            if d.is_user:
                chat_type = ChatType.USER
            elif d.is_channel:
                if getattr(d.entity, 'forum', False):
                    chat_type = ChatType.FORUM
                elif not d.is_group:
                    chat_type = ChatType.CHANNEL

            unread_topics_count = None
            calculated_unread_count = d.unread_count
            topic_map = {}

            if chat_type == ChatType.FORUM:
                try:
                    response = await self.client(functions.messages.GetForumTopicsRequest(
                        peer=d.entity,
                        offset_date=None,
                        offset_id=0,
                        offset_topic=0,
                        limit=20,
                        q=''
                    ))

                    active_topics = response.topics
                    for t in active_topics:
                        topic_map[t.id] = t.title

                    unread_topics = [t for t in active_topics if t.unread_count > 0]
                    if unread_topics:
                        unread_topics_count = len(unread_topics)
                        calculated_unread_count = sum(t.unread_count for t in unread_topics)

                except Exception as e:
                    print(f"Failed to fetch topics for forum {d.name}: {e}")

            preview_topic_name = None
            msg = getattr(d, 'message', None)
            if chat_type == ChatType.FORUM and msg:
                reply_to = getattr(msg, 'reply_to', None)
                if reply_to:
                    tid = getattr(reply_to, 'reply_to_msg_id', None) or getattr(reply_to, 'reply_to_top_id', None)
                    preview_topic_name = topic_map.get(tid)

            preview = self._format_preview(msg, chat_type, preview_topic_name)
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
            response = await self.client(functions.messages.GetForumTopicsRequest(
                peer=entity,
                offset_date=None,
                offset_id=0,
                offset_topic=0,
                limit=limit,
                q=''
            ))

            topics = []
            top_message_ids = [t.top_message for t in response.topics]
            messages_map = {}

            if top_message_ids:
                try:
                    msgs = await self.client.get_messages(entity, ids=top_message_ids)
                    if msgs:
                        for m in msgs:
                            if m:
                                messages_map[m.id] = m
                except Exception:
                    pass

            for t in response.topics:
                last_msg = messages_map.get(t.top_message)
                preview = self._format_preview(last_msg, ChatType.GROUP)

                topics.append(Chat(
                    id=t.id,
                    name=t.title,
                    unread_count=t.unread_count,
                    type=ChatType.TOPIC,
                    last_message_preview=preview,
                    image_url=None
                ))

            return topics
        except Exception as e:
            print(f"Error fetching forum topics: {e}")
            return []
