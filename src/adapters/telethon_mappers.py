from typing import Any, Optional, Dict
from telethon.tl.types import MessageService, MessageMediaPhoto, MessageMediaDocument, DocumentAttributeSticker, DocumentAttributeVideo, DocumentAttributeAudio
from telethon import utils
from html import unescape as html_unescape
from src.domain.models import ChatType

def map_telethon_dialog_to_chat_type(d: Any) -> ChatType:
    if d.is_user:
        return ChatType.USER
    elif d.is_channel:
        if getattr(d.entity, 'forum', False):
            return ChatType.FORUM
        elif not d.is_group:
            return ChatType.CHANNEL
    return ChatType.GROUP

def format_message_preview(message: Any, chat_type: ChatType, topic_map: Optional[Dict[int, str]] = None) -> str:
    if not message:
        return "No messages"

    if isinstance(message, MessageService):
        return "Service message"

    text = getattr(message, 'message', '')

    # Handle Media Previews
    media = getattr(message, 'media', None)
    media_text = ""

    if media:
        if isinstance(media, MessageMediaPhoto):
            media_text = "📷 Photo"
        elif isinstance(media, MessageMediaDocument):
            if hasattr(media, 'document'):
                # Check attributes
                for attr in getattr(media.document, 'attributes', []):
                    if isinstance(attr, DocumentAttributeSticker):
                        emoji = attr.alt or ""
                        media_text = f"{emoji} Sticker" if emoji else "Sticker"
                        break
                    elif isinstance(attr, DocumentAttributeVideo):
                        media_text = "📹 Video"
                        break
                    elif isinstance(attr, DocumentAttributeAudio):
                        if getattr(attr, 'voice', False):
                            media_text = "🎤 Voice Message"
                        else:
                            performer = getattr(attr, 'performer', '')
                            title = getattr(attr, 'title', '')
                            if performer and title:
                                media_text = f"🎵 {performer} - {title}"
                            elif title:
                                media_text = f"🎵 {title}"
                            else:
                                media_text = "🎵 Music"
                        break

                # If still empty, generic document
                if not media_text:
                    media_text = "📄 Document"

    # If text is present, show text. If text is empty, show media description.
    if not text and media_text:
        text = media_text
    elif text and media_text:
        # Prioritize text but maybe indicator? For now just text
        pass
    elif not text and not media_text:
        text = "Media/Sticker"

    text = html_unescape(text) # UNESCAPE HTML entities like &quot;
    text = text.replace('\n', ' ')

    MAX_LENGTH = 100
    if len(text) > MAX_LENGTH:
        text = text[:(MAX_LENGTH - 3)] + "..."

    prefix = ""
    if chat_type == ChatType.GROUP:
        sender = getattr(message, 'sender', None)
        if sender:
            name = utils.get_display_name(sender)
            prefix = f"{name}: "
    elif chat_type == ChatType.FORUM and topic_map and message:
        reply_to = getattr(message, 'reply_to', None)
        if reply_to:
            tid = getattr(reply_to, 'reply_to_msg_id', None) or getattr(reply_to, 'reply_to_top_id', None)
            if tid is not None:
                topic_name = topic_map.get(tid)
                if topic_name:
                     prefix = f"{topic_name}: "
    elif chat_type == ChatType.TOPIC:
        sender = getattr(message, 'sender', None)
        if sender:
            name = utils.get_display_name(sender)
            prefix = f"{name}: "


    return f"{prefix}{text}"
