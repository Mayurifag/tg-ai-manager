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
    sender_username: Optional[str] = None
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
    audio_duration: Optional[int] = None  # Seconds
    # Poll fields
    is_poll: bool = False
    poll_question: Optional[str] = None
    # Service Messages
    is_service: bool = False

    def get_preview_text(self) -> str:
        """Returns a text representation of the message, handling media fallbacks."""
        if self.text:
            return self.text

        if self.has_media:
            if self.is_sticker:
                return f"{self.sticker_emoji or ''} Sticker"
            elif self.is_video:
                return "📹 Video"
            elif self.is_audio:
                if self.is_voice:
                    return "🎤 Voice"
                performer = self.audio_performer or ""
                title = self.audio_title or "Music"
                return f"🎵 {performer} - {title}" if performer else f"🎵 {title}"
            elif self.is_poll:
                return f"Poll: {self.poll_question or 'Unknown Poll'}"
            return "📷 Media"

        return "Message"


@dataclass
class SystemEvent:
    type: str  # "message", "edited", "deleted", "action"
    text: str
    chat_name: str
    topic_name: Optional[str] = None
    date: datetime = field(default_factory=datetime.now)
    chat_id: Optional[int] = None
    topic_id: Optional[int] = None
    link: Optional[str] = None
    message_model: Optional[Message] = None
    rendered_html: Optional[str] = None
    is_read: bool = False


@dataclass
class ActionLog:
    action: str
    chat_id: int
    chat_name: str
    reason: str
    date: datetime = field(default_factory=datetime.now)
    link: Optional[str] = None
    id: Optional[int] = None
