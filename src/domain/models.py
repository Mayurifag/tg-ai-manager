from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from html import escape as html_escape
from typing import List, Optional


class ChatType(str, Enum):
    USER = "user"
    GROUP = "group"
    CHANNEL = "channel"
    FORUM = "forum"
    TOPIC = "topic"


@dataclass(frozen=True)
class MediaContent:
    # Documents are streamed; image previews are held in memory.
    data: Optional[bytes]
    mime_type: str


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
    last_message_id: Optional[int] = None


@dataclass(frozen=True)
class ForumTopicCursor:
    date: datetime
    message_id: int
    topic_id: int


@dataclass
class ForumTopicPage:
    topics: List[Chat]
    next_cursor: Optional[ForumTopicCursor]


@dataclass
class Reaction:
    emoji: str
    count: int
    is_chosen: bool
    custom_emoji_id: Optional[int] = None


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
    is_photo: bool = False
    is_video: bool = False
    media_width: Optional[int] = None
    media_height: Optional[int] = None
    is_sticker: bool = False
    sticker_emoji: Optional[str] = None
    is_document: bool = False
    file_name: Optional[str] = None
    file_size: Optional[int] = None
    webpage_url: Optional[str] = None
    webpage_title: Optional[str] = None
    webpage_description: Optional[str] = None
    forwarded_from: Optional[str] = None
    # Audio fields
    is_audio: bool = False
    is_voice: bool = False
    audio_title: Optional[str] = None
    audio_performer: Optional[str] = None
    audio_duration: Optional[int] = None  # Seconds
    # Poll fields
    is_poll: bool = False
    poll_question: Optional[str] = None
    poll_options: List[str] = field(default_factory=list)
    # Service Messages
    is_service: bool = False
    # Reactions
    reactions: List[Reaction] = field(default_factory=list)
    # Grouping (Albums)
    grouped_id: Optional[int] = None
    album_parts: Optional[List["Message"]] = None  # Holds other messages in the group
    # Raw Telegram text is for decisions; text contains sanitized presentation HTML.
    raw_text: Optional[str] = None

    @property
    def media_geometry(self) -> tuple[int, int, int]:
        # shortcut: unknown or >20:1 dimensions use a 4:3 placeholder;
        # revisit if legitimate Telegram media needs more extreme ratios.
        width, height = self.media_width, self.media_height
        if (
            type(width) is not int
            or type(height) is not int
            or not 0 < width <= 100_000
            or not 0 < height <= 100_000
            or width > height * 20
            or height > width * 20
        ):
            return 320, 4, 3
        return min(520, width, max(1, 480 * width // height)), width, height

    def get_preview_text(self) -> str:
        """Returns a text representation of the message, handling media fallbacks."""
        if self.text:
            return self.text

        if self.has_media:
            if self.is_sticker:
                return f"{html_escape(self.sticker_emoji or '')} Sticker"
            elif self.is_video:
                return "📹 Video"
            elif self.is_audio:
                if self.is_voice:
                    return "🎤 Voice"
                performer = self.audio_performer or ""
                title = self.audio_title or "Music"
                return html_escape(
                    f"🎵 {performer} - {title}" if performer else f"🎵 {title}"
                )
            elif self.is_poll:
                return html_escape(f"Poll: {self.poll_question or 'Unknown Poll'}")
            elif self.is_document:
                return html_escape(f"📄 {self.file_name or 'File'}")
            elif self.webpage_title:
                return html_escape(self.webpage_title)
            elif self.album_parts:
                return "📷 Album"
            return "📷 Media"

        return "Message"


@dataclass(frozen=True)
class MessageContext:
    message: Message
    topic_id: Optional[int]
    scope_known: bool


@dataclass
class SystemEvent:
    type: str  # "message", "edited", "deleted", "action", "reaction_update"
    text: str
    chat_name: str
    topic_name: Optional[str] = None
    date: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    chat_id: Optional[int] = None
    topic_id: Optional[int] = None
    link: Optional[str] = None
    message_model: Optional[Message] = None
    rendered_html: Optional[str] = None
    is_read: bool = False
    scope_known: bool = True
    read_origin: Optional[str] = None
    read_through: Optional[int] = None


@dataclass
class ActionLog:
    action: str
    chat_id: int
    chat_name: str
    reason: str
    date: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    link: Optional[str] = None
    id: Optional[int] = None
