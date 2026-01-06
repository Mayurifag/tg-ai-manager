from datetime import datetime
from typing import Optional, Dict, Any
from html import escape as html_escape
from telethon import utils
from telethon.extensions import html
from telethon.tl import types
from telethon.tl.types import (
    MessageMediaPoll,
    MessageMediaDocument,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    DocumentAttributeAudio,
)
from src.domain.models import Message
from src.adapters.telethon_mappers import get_message_action_text


class MessageParserMixin:
    def __init__(self):
        self.client: Any = None
        self._msg_id_to_chat_id: Dict[int, int] = {}

    async def _get_chat_image(self, entity: Any, chat_id: int) -> Optional[str]:
        raise NotImplementedError

    def _extract_topic_id(self, message: Any) -> Optional[int]:
        reply_header = getattr(message, "reply_to", None)
        if reply_header:
            tid = getattr(reply_header, "reply_to_top_id", None)
            if not tid:
                tid = getattr(reply_header, "reply_to_msg_id", None)
            return tid
        return None

    def _cache_message_chat(self, msg_id: int, chat_id: int):
        if len(self._msg_id_to_chat_id) > 15000:
            self._msg_id_to_chat_id.clear()
        self._msg_id_to_chat_id[msg_id] = chat_id

    def _get_sender_color(self, sender: Any, sender_id: int) -> str:
        color_index = 0
        peer_color = getattr(sender, "color", None)
        if peer_color and hasattr(peer_color, "color"):
            color_index = peer_color.color
        else:
            color_index = abs(sender_id) % 7
        palette = [
            "#e17076",
            "#faa774",
            "#a695e7",
            "#7bc862",
            "#6ec9cb",
            "#65aadd",
            "#ee7aae",
        ]
        return palette[color_index % 7]

    def _get_sender_initials(self, name: str) -> str:
        if not name:
            return "?"
        parts = name.strip().split()
        if not parts:
            return "?"
        first = parts[0][0]
        second = parts[1][0] if len(parts) > 1 else ""
        return (first + second).upper()

    def _extract_text(self, msg: Any) -> str:
        raw_text = getattr(msg, "message", "") or ""
        entities = getattr(msg, "entities", [])
        if raw_text:
            try:
                return html.unparse(raw_text, entities or [])
            except Exception:
                return html_escape(raw_text)
        return ""

    async def _parse_message(
        self,
        msg: Any,
        replies_map: Dict[int, Any] | None = None,
        chat_id: Optional[int] = None,
    ) -> Message:
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
        media = getattr(msg, "media", None)
        has_media = bool(media)
        is_video = False
        is_sticker = False
        sticker_emoji = None

        # Audio
        is_audio = False
        is_voice = False
        audio_title = None
        audio_performer = None
        audio_duration = None

        # Poll fields
        is_poll = False
        poll_question = None

        if has_media:
            if isinstance(media, MessageMediaPoll):
                is_poll = True
                poll = media.poll
                poll_question = getattr(poll.question, "text", "Poll")
            elif isinstance(media, MessageMediaDocument):
                for attr in getattr(media.document, "attributes", []):
                    if isinstance(attr, DocumentAttributeVideo):
                        is_video = True
                    elif isinstance(attr, DocumentAttributeSticker):
                        is_sticker = True
                        sticker_emoji = attr.alt
                    elif isinstance(attr, DocumentAttributeAudio):
                        is_audio = True
                        is_voice = getattr(attr, "voice", False)
                        audio_title = getattr(attr, "title", None)
                        audio_performer = getattr(attr, "performer", None)
                        audio_duration = getattr(attr, "duration", None)

        sender_name = "Unknown"
        sender_id = 0
        sender_username = None
        avatar_url = None
        sender_initials = "?"
        sender_color = "#ccc"

        if getattr(msg, "from_id", None):
            if isinstance(msg.from_id, types.PeerUser):
                sender_id = msg.from_id.user_id
            elif isinstance(msg.from_id, types.PeerChannel):
                sender_id = msg.from_id.channel_id

        sender = getattr(msg, "sender", None)
        if sender_id and not sender:
            try:
                sender = await self.client.get_entity(sender_id)
            except Exception:
                pass

        if sender:
            sender_id = sender.id
            sender_name = utils.get_display_name(sender)
            sender_username = getattr(sender, "username", None)
            avatar_url = await self._get_chat_image(sender, sender_id)

        sender_color = self._get_sender_color(sender, sender_id)
        sender_initials = self._get_sender_initials(sender_name)

        reply_to_msg_id = None
        reply_to_text = None
        reply_to_sender = None
        reply_header = getattr(msg, "reply_to", None)

        if reply_header:
            reply_to_msg_id = getattr(reply_header, "reply_to_msg_id", None)
            if reply_to_msg_id and reply_to_msg_id in replies_map:
                r_msg = replies_map[reply_to_msg_id]
                r_text = self._extract_text(r_msg)
                if not r_text:
                    r_text = "📷 Media"
                reply_to_text = r_text
                r_sender = getattr(r_msg, "sender", None)
                reply_to_sender = (
                    utils.get_display_name(r_sender) if r_sender else "User"
                )

        return Message(
            id=msg.id,
            text=text,
            date=msg.date or datetime.now(),
            sender_name=sender_name,
            is_outgoing=getattr(msg, "out", False),
            sender_id=sender_id,
            sender_username=sender_username,
            avatar_url=avatar_url,
            sender_color=sender_color,
            sender_initials=sender_initials,
            reply_to_msg_id=reply_to_msg_id,
            reply_to_text=reply_to_text,
            reply_to_sender_name=reply_to_sender,
            has_media=has_media,
            is_video=is_video,
            is_sticker=is_sticker,
            sticker_emoji=sticker_emoji,
            is_audio=is_audio,
            is_voice=is_voice,
            audio_title=audio_title,
            audio_performer=audio_performer,
            audio_duration=audio_duration,
            is_poll=is_poll,
            poll_question=poll_question,
            is_service=is_service,
        )
