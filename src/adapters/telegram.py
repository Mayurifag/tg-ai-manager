import os
import asyncio
from datetime import datetime
from typing import Protocol, Any, Awaitable, List, Optional, Dict, Callable
from html import escape as html_escape
from telethon import TelegramClient, functions, utils, events
from telethon.extensions import html
from telethon.tl import types
from telethon.tl.functions.messages import GetPeerDialogsRequest, GetForumTopicsRequest
from telethon.tl.types import InputDialogPeer, MessageMediaPhoto, MessageMediaDocument, DocumentAttributeSticker, DocumentAttributeVideo, DocumentAttributeAudio
from src.domain.ports import ChatRepository
from src.domain.models import Chat, ChatType, Message, SystemEvent
from src.adapters.telethon_mappers import map_telethon_dialog_to_chat_type, format_message_preview, get_message_action_text

class ITelethonClient(Protocol):
    def connect(self) -> Awaitable[None]: ...
    def disconnect(self) -> Awaitable[None]: ...
    def is_user_authorized(self) -> Awaitable[bool]: ...
    def is_connected(self) -> bool: ...
    def start(self) -> Awaitable[Any]: ...
    def get_dialogs(self, limit: int) -> Awaitable[Any]: ...
    def get_messages(self, entity: Any, limit: int = 20, reply_to: int | None = None, ids: List[int] | None = None, offset_id: int = 0) -> Awaitable[Any]: ...
    def get_entity(self, entity: Any) -> Awaitable[Any]: ...
    def download_profile_photo(self, entity: Any, file: Any = None, download_big: bool = False) -> Awaitable[Any]: ...
    def download_media(self, message: Any, file: Any = None, thumb: Any = None) -> Awaitable[Any]: ...
    def __call__(self, request: Any) -> Awaitable[Any]: ...
    def add_event_handler(self, callback: Any, event: Any): ...

class TelethonAdapter(ChatRepository):
    def __init__(self, session_name: str, api_id: int, api_hash: str):
        self.client: ITelethonClient = TelegramClient(session_name, api_id, api_hash) # type: ignore
        self.images_dir = os.path.join(os.getcwd(), "cache")
        self.listeners: List[Callable[[SystemEvent], Awaitable[None]]] = []
        self._event_handler_registered = False
        self._msg_id_to_chat_id: Dict[int, int] = {}

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

    def add_event_listener(self, callback: Callable[[SystemEvent], Awaitable[None]]):
        self.listeners.append(callback)

    async def _dispatch(self, event: SystemEvent):
        for listener in self.listeners:
            try:
                await listener(event)
            except Exception as e:
                print(f"Error in event listener: {e}")

    def _extract_topic_id(self, message: Any) -> Optional[int]:
        reply_header = getattr(message, 'reply_to', None)
        if reply_header:
            tid = getattr(reply_header, 'reply_to_top_id', None)
            if not tid:
                tid = getattr(reply_header, 'reply_to_msg_id', None)
            return tid
        return None

    def _cache_message_chat(self, msg_id: int, chat_id: int):
        if len(self._msg_id_to_chat_id) > 15000:
            self._msg_id_to_chat_id.clear()
        self._msg_id_to_chat_id[msg_id] = chat_id

    async def _handle_new_message(self, event):
        try:
            if event.chat_id:
                self._cache_message_chat(event.message.id, event.chat_id)

            chat_name = "Unknown"
            try:
                chat = await event.get_chat()
                chat_name = utils.get_display_name(chat)
            except: pass

            domain_msg = await self._parse_message(event.message, chat_id=event.chat_id)
            topic_id = self._extract_topic_id(event.message)

            preview = domain_msg.text
            if not preview and domain_msg.has_media:
                 if domain_msg.is_sticker: preview = f"{domain_msg.sticker_emoji or ''} Sticker"
                 elif domain_msg.is_video: preview = "📹 Video"
                 elif domain_msg.is_audio:
                     if domain_msg.is_voice: preview = "🎤 Voice"
                     else: preview = f"🎵 {domain_msg.audio_performer or ''} - {domain_msg.audio_title or 'Music'}"
                 else: preview = "📷 Media"

            if len(preview) > 75:
                 preview = preview[:75] + '...'

            sys_event = SystemEvent(
                type="message",
                text=preview,
                chat_name=chat_name,
                chat_id=event.chat_id,
                topic_id=topic_id,
                link=f"/chat/{event.chat_id}",
                message_model=domain_msg
            )
            await self._dispatch(sys_event)
        except Exception as e:
            print(f"Error handling new message: {e}")

    async def _handle_edited_message(self, event):
        try:
            if event.chat_id:
                self._cache_message_chat(event.message.id, event.chat_id)

            chat_name = "Unknown"
            try:
                chat = await event.get_chat()
                chat_name = utils.get_display_name(chat)
            except: pass

            domain_msg = await self._parse_message(event.message, chat_id=event.chat_id)
            preview = domain_msg.text
            if not preview and domain_msg.has_media:
                 preview = "Media/Sticker"
            if len(preview) > 75: preview = preview[:75] + '...'

            topic_id = self._extract_topic_id(event.message)

            sys_event = SystemEvent(
                type="edited",
                text=preview,
                chat_name=chat_name,
                chat_id=event.chat_id,
                topic_id=topic_id,
                link=f"/chat/{event.chat_id}",
                message_model=domain_msg
            )
            await self._dispatch(sys_event)
        except Exception as e:
            print(f"Error handling edit: {e}")

    async def _handle_deleted_message(self, event):
        try:
            chat_id = getattr(event, 'chat_id', None)
            if not chat_id and event.deleted_ids:
                for did in event.deleted_ids:
                    if did in self._msg_id_to_chat_id:
                        chat_id = self._msg_id_to_chat_id[did]
                        break

            if not chat_id and hasattr(event, 'original_update'):
                 if hasattr(event.original_update, 'channel_id'):
                    chat_id = utils.get_peer_id(types.PeerChannel(event.original_update.channel_id))

            chat_name = "Unknown"
            if chat_id:
                try:
                    entity = await self.client.get_entity(chat_id)
                    chat_name = utils.get_display_name(entity)
                except:
                    chat_name = f"Chat {chat_id}"
            else:
                print(f"DEBUG: Could not resolve chat_id for deleted messages: {event.deleted_ids}")

            if chat_id and event.deleted_ids:
                sys_event = SystemEvent(
                    type="deleted",
                    text="Message deleted",
                    chat_name=chat_name,
                    chat_id=chat_id,
                    link=f"/chat/{chat_id}" if chat_id else "#",
                    message_model=Message(
                        id=event.deleted_ids[0], text="", date=datetime.now(),
                        sender_name="", is_outgoing=False
                    )
                )
                await self._dispatch(sys_event)

        except Exception as e:
            print(f"Error handling delete: {e}")

    async def _handle_chat_action(self, event):
        try:
            chat_name = "Unknown"
            try:
                chat = await event.get_chat()
                chat_name = utils.get_display_name(chat)
            except: pass

            # Use the strict mapper logic to get text, but we need the 'SystemEvent' to work.
            # event.action_message is the MessageService object
            text = "Unknown action"

            # Re-use our centralized mapper
            action_text = get_message_action_text(event.action_message)
            if action_text:
                # Add sender name if available to make it "Sender pinned a message"
                # event.action_message has sender info
                msg_model = await self._parse_message(event.action_message, chat_id=event.chat_id)
                text = f"{msg_model.sender_name} {action_text}"

            sys_event = SystemEvent(
                type="action",
                text=text,
                chat_name=chat_name,
                chat_id=event.chat_id,
                link=f"/chat/{event.chat_id}"
            )
            await self._dispatch(sys_event)
        except Exception as e:
            print(f"Error handling action: {e}")

    async def _get_chat_image(self, entity: Any, chat_id: int) -> Optional[str]:
        filename = f"{chat_id}.jpg"
        path = os.path.join(self.images_dir, filename)
        if os.path.exists(path): return f"/cache/{filename}"
        try:
            result = await self.client.download_profile_photo(entity, file=path, download_big=False)
            if result: return f"/cache/{filename}"
        except Exception: pass
        return None

    async def download_media(self, chat_id: int, message_id: int) -> Optional[str]:
        try:
            entity = await self.client.get_entity(chat_id)
            message = await self.client.get_messages(entity, ids=message_id)

            if not message or not message.media:
                return None

            ext = "jpg"
            guessed_ext = utils.get_extension(message.media)
            if guessed_ext:
                ext = guessed_ext.lstrip('.')

            if hasattr(message.media, 'document'):
                 if hasattr(message.media.document, 'mime_type'):
                     if 'webp' in message.media.document.mime_type: ext = "webp"
                     elif 'audio/ogg' in message.media.document.mime_type: ext = "ogg"
                     elif 'audio/mpeg' in message.media.document.mime_type: ext = "mp3"
                     elif 'video/mp4' in message.media.document.mime_type: ext = "mp4"

            filename = f"media_{chat_id}_{message_id}.{ext}"
            path = os.path.join(self.images_dir, filename)

            if os.path.exists(path):
                return f"/cache/{filename}"

            is_sticker = False
            is_audio = False
            is_video = False

            if isinstance(message.media, MessageMediaDocument):
                 for attr in getattr(message.media.document, 'attributes', []):
                      if isinstance(attr, DocumentAttributeSticker): is_sticker = True
                      if isinstance(attr, DocumentAttributeAudio): is_audio = True
                      if isinstance(attr, DocumentAttributeVideo): is_video = True

            result = None
            if is_sticker or is_audio or is_video:
                 result = await self.client.download_media(message, file=path)
            else:
                 result = await self.client.download_media(message, file=path, thumb='m')
                 if not result:
                     result = await self.client.download_media(message, file=path)

            if result:
                 final_filename = os.path.basename(result)
                 return f"/cache/{final_filename}"

        except Exception as e:
            print(f"Error downloading media {chat_id}/{message_id}: {e}")
        return None

    def _get_sender_color(self, sender: Any, sender_id: int) -> str:
        color_index = 0
        peer_color = getattr(sender, 'color', None)
        if peer_color and hasattr(peer_color, 'color'): color_index = peer_color.color
        else: color_index = abs(sender_id) % 7
        palette = ["#e17076", "#faa774", "#a695e7", "#7bc862", "#6ec9cb", "#65aadd", "#ee7aae"]
        return palette[color_index % 7]

    def _get_sender_initials(self, name: str) -> str:
        if not name: return "?"
        parts = name.strip().split()
        if not parts: return "?"
        first = parts[0][0]
        second = parts[1][0] if len(parts) > 1 else ""
        return (first + second).upper()

    def _extract_text(self, msg: Any) -> str:
        raw_text = getattr(msg, 'message', '') or ""
        entities = getattr(msg, 'entities', [])
        if raw_text:
            try: return html.unparse(raw_text, entities or [])
            except Exception: return html_escape(raw_text)
        return ""

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
            except Exception as e: pass
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
            if msg:
                self._cache_message_chat(msg.id, d.id)

            preview = format_message_preview(msg, chat_type, topic_map)
            image_url = await self._get_chat_image(d.entity, d.id)

            results.append(Chat(
                id=d.id, name=d.name, unread_count=calculated_unread_count,
                type=chat_type, unread_topics_count=unread_topics_count,
                last_message_preview=preview, image_url=image_url,
                is_pinned=d.pinned
            ))
        return results

    async def get_chat(self, chat_id: int) -> Optional[Chat]:
        try:
            entity = await self.client.get_entity(chat_id)
            name = utils.get_display_name(entity)

            c_type = ChatType.GROUP
            if isinstance(entity, types.User): c_type = ChatType.USER
            elif isinstance(entity, types.Chat): c_type = ChatType.GROUP
            elif isinstance(entity, types.Channel):
                if getattr(entity, 'forum', False): c_type = ChatType.FORUM
                elif getattr(entity, 'broadcast', False): c_type = ChatType.CHANNEL
                else: c_type = ChatType.GROUP

            image_url = await self._get_chat_image(entity, chat_id)

            unread_count = 0
            unread_topics_count = None
            last_message_preview = None
            is_pinned = False

            try:
                input_peer = utils.get_input_peer(entity)
                res = await self.client(GetPeerDialogsRequest(peers=[InputDialogPeer(peer=input_peer)])) # type: ignore
                if res.dialogs:
                    dialog = res.dialogs[0]
                    unread_count = dialog.unread_count
                    is_pinned = dialog.pinned
            except Exception as e:
                print(f"Warning: Could not fetch dialog stats for {chat_id}: {e}")

            try:
                messages = await self.client.get_messages(entity, limit=1)
                if messages:
                    latest_msg = messages[0]
                    self._cache_message_chat(latest_msg.id, chat_id) # Update Cache
                    last_message_preview = format_message_preview(latest_msg, c_type, {})
                else:
                    last_message_preview = "No messages"
            except Exception as e:
                print(f"Warning: Could not fetch latest message for {chat_id}: {e}")

            if c_type == ChatType.FORUM:
                try:
                    topics_res = await self._fetch_forum_topics_response(entity, limit=20)
                    if topics_res:
                        unread_topics = [t for t in topics_res.topics if t.unread_count > 0]
                        if unread_topics:
                            unread_topics_count = len(unread_topics)
                            unread_count = sum(t.unread_count for t in unread_topics)
                except Exception as e:
                    print(f"Warning: Could not fetch forum stats: {e}")

            return Chat(
                id=chat_id,
                name=name,
                unread_count=unread_count,
                type=c_type,
                unread_topics_count=unread_topics_count,
                image_url=image_url,
                last_message_preview=last_message_preview,
                is_pinned=is_pinned
            )
        except Exception as e:
            print(f"Error fetching chat info {chat_id}: {e}")
            return None

    async def _parse_message(self, msg: Any, replies_map: Dict[int, Any] | None = None, chat_id: Optional[int] = None) -> Message:
        if chat_id:
            self._cache_message_chat(msg.id, chat_id)

        replies_map = replies_map or {}

        # --- Service Message Logic ---
        is_service = False
        text = ""

        if isinstance(msg, types.MessageService):
            is_service = True
            text = get_message_action_text(msg) or "Service message"
        else:
            text = self._extract_text(msg)

        # Check Media
        has_media = bool(msg.media)
        is_video = False
        is_sticker = False
        sticker_emoji = None

        # Audio
        is_audio = False
        is_voice = False
        audio_title = None
        audio_performer = None
        audio_duration = None

        if has_media:
            if isinstance(msg.media, MessageMediaDocument):
                for attr in getattr(msg.media.document, 'attributes', []):
                    if isinstance(attr, DocumentAttributeVideo):
                        is_video = True
                    elif isinstance(attr, DocumentAttributeSticker):
                        is_sticker = True
                        sticker_emoji = attr.alt
                    elif isinstance(attr, DocumentAttributeAudio):
                        is_audio = True
                        is_voice = getattr(attr, 'voice', False)
                        audio_title = getattr(attr, 'title', None)
                        audio_performer = getattr(attr, 'performer', None)
                        audio_duration = getattr(attr, 'duration', None)

        sender_name = "Unknown"
        sender_id = 0
        avatar_url = None
        sender_initials = "?"

        if getattr(msg, 'from_id', None):
             if isinstance(msg.from_id, types.PeerUser): sender_id = msg.from_id.user_id
             elif isinstance(msg.from_id, types.PeerChannel): sender_id = msg.from_id.channel_id

        sender = getattr(msg, 'sender', None)
        if sender_id and not sender:
             try: sender = await self.client.get_entity(sender_id)
             except Exception: pass

        if sender:
             sender_id = sender.id
             sender_name = utils.get_display_name(sender)
             avatar_url = await self._get_chat_image(sender, sender_id)

        sender_color = self._get_sender_color(sender, sender_id)
        sender_initials = self._get_sender_initials(sender_name)

        reply_to_msg_id = None
        reply_to_text = None
        reply_to_sender = None
        reply_header = getattr(msg, 'reply_to', None)

        if reply_header:
            reply_to_msg_id = getattr(reply_header, 'reply_to_msg_id', None)
            if reply_to_msg_id and reply_to_msg_id in replies_map:
                r_msg = replies_map[reply_to_msg_id]
                r_text = self._extract_text(r_msg)
                if not r_text: r_text = "📷 Media"
                reply_to_text = r_text
                r_sender = getattr(r_msg, 'sender', None)
                reply_to_sender = utils.get_display_name(r_sender) if r_sender else "User"

        return Message(
            id=msg.id, text=text, date=msg.date, sender_name=sender_name,
            is_outgoing=getattr(msg, 'out', False), sender_id=sender_id,
            avatar_url=avatar_url, sender_color=sender_color,
            sender_initials=sender_initials, reply_to_msg_id=reply_to_msg_id,
            reply_to_text=reply_to_text, reply_to_sender_name=reply_to_sender,
            has_media=has_media, is_video=is_video, is_sticker=is_sticker,
            sticker_emoji=sticker_emoji,
            is_audio=is_audio, is_voice=is_voice,
            audio_title=audio_title, audio_performer=audio_performer, audio_duration=audio_duration,
            is_service=is_service
        )

    async def get_messages(self, chat_id: int, limit: int = 20, topic_id: Optional[int] = None, offset_id: int = 0) -> List[Message]:
        try:
            entity = await self.client.get_entity(chat_id)
            messages = await self.client.get_messages(entity, limit=limit, reply_to=topic_id, offset_id=offset_id)
            reply_ids = []
            for msg in messages:
                self._cache_message_chat(msg.id, chat_id)
                reply_header = getattr(msg, 'reply_to', None)
                if reply_header:
                     rid = getattr(reply_header, 'reply_to_msg_id', None)
                     if rid: reply_ids.append(rid)

            replies_map = {}
            if reply_ids:
                try:
                    reply_ids = list(set(reply_ids))
                    replied_msgs = await self.client.get_messages(entity, ids=reply_ids)
                    for r in replied_msgs:
                        if r: replies_map[r.id] = r
                except Exception as e: pass

            result_messages = []
            for msg in messages:
                parsed = await self._parse_message(msg, replies_map, chat_id=chat_id)
                result_messages.append(parsed)
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
