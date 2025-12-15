from dataclasses import dataclass
from typing import Optional
from enum import Enum
from datetime import datetime

class ChatType(str, Enum):
    USER = "user"
    GROUP = "group"
    CHANNEL = "channel"
    FORUM = "forum"
    TOPIC = "topic"

@dataclass
class Chat:
    id: int
    name: str
    unread_count: int
    type: ChatType
    unread_topics_count: Optional[int] = None
    last_message_preview: Optional[str] = None
    image_url: Optional[str] = None
    icon_emoji: Optional[str] = None

@dataclass
class Message:
    id: int
    text: str
    date: datetime
    sender_name: str
    is_outgoing: bool
    sender_id: Optional[int] = None
    avatar_url: Optional[str] = None
    sender_color: Optional[str] = None
    sender_initials: Optional[str] = None
    reply_to_msg_id: Optional[int] = None
    reply_to_text: Optional[str] = None
    reply_to_sender_name: Optional[str] = None
