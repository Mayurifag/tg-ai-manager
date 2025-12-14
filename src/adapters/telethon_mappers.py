from typing import Any, Optional, Dict
from telethon.tl.types import MessageService
from telethon import utils
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
    elif chat_type == ChatType.FORUM and topic_map and message:
        reply_to = getattr(message, 'reply_to', None)
        if reply_to:
            tid = getattr(reply_to, 'reply_to_msg_id', None) or getattr(reply_to, 'reply_to_top_id', None)
            topic_name = topic_map.get(tid)
            if topic_name:
                 prefix = f"{topic_name}: "
    elif chat_type == ChatType.TOPIC:
        sender = getattr(message, 'sender', None)
        if sender:
            name = utils.get_display_name(sender)
            prefix = f"{name}: "


    return f"{prefix}{text}"
