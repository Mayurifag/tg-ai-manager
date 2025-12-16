from dataclasses import dataclass, field
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
    is_pinned: bool = False

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
    # Media fields
    has_media: bool = False
    is_video: bool = False
    is_sticker: bool = False
    sticker_emoji: Optional[str] = None
    # Audio fields
    is_audio: bool = False
    is_voice: bool = False
    audio_title: Optional[str] = None
    audio_performer: Optional[str] = None
    audio_duration: Optional[int] = None # Seconds
    # Service Messages
    is_service: bool = False

@dataclass
class SystemEvent:
    type: str  # "message", "edited", "deleted", "action"
    text: str
    chat_name: str
    date: datetime = field(default_factory=datetime.now)
    chat_id: Optional[int] = None
    topic_id: Optional[int] = None
    link: Optional[str] = None
    message_model: Optional[Message] = None
    rendered_html: Optional[str] = None

@dataclass
class ActionLog:
    action: str
    chat_id: int
    chat_name: str
    reason: str
    date: datetime = field(default_factory=datetime.now)
    link: Optional[str] = None
    id: Optional[int] = None
