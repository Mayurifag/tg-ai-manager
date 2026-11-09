import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from html import escape as html_escape
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set
from urllib.parse import urlsplit

from telethon import utils
from telethon.extensions import html
from telethon.tl import types
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaPoll,
    MessageMediaWebPage,
    PeerChannel,
    PeerChat,
    PeerUser,
    ReactionCustomEmoji,
    ReactionEmoji,
)

from src.adapters.telethon_mappers import get_message_action_text
from src.domain.errors import AccountChanged
from src.domain.models import Message, Reaction
from src.infrastructure.html import sanitize_html
from src.infrastructure.logging import get_logger
from src.infrastructure.tasks import raise_if_task_stopped, run_with_task_deadline

if TYPE_CHECKING:
    from src.adapters.telegram.media import AvatarGeneration, MediaManager

logger = get_logger(__name__)
# shortcut: allow ten seconds for optional sender details; revisit if normal
# Telegram lookups routinely take longer.
SENDER_LOOKUP_TIMEOUT = 10.0


def media_dimensions(value: Any) -> tuple[Optional[int], Optional[int]]:
    width = getattr(value, "w", None)
    height = getattr(value, "h", None)
    if (
        type(width) is int
        and type(height) is int
        and 0 < width <= 100_000
        and 0 < height <= 100_000
    ):
        return width, height
    return None, None


class MessageParser:
    def __init__(self, client: Any, media: "MediaManager") -> None:
        self.client = client
        self._media = media
        # Only non-channel message IDs are account-scoped. Channel IDs can
        # collide with each other and with private/basic-group message IDs.
        self._msg_id_to_chat_id: Dict[int, Optional[int]] = {}
        self.self_id: Optional[int] = None

    def _extract_topic_id(self, message: Any) -> Optional[int]:
        """Identify a topic for a message already known to belong to a forum."""
        if isinstance(getattr(message, "action", None), types.MessageActionTopicCreate):
            return getattr(message, "id", None)
        reply_header = getattr(message, "reply_to", None)
        if reply_header and getattr(reply_header, "forum_topic", False):
            return getattr(reply_header, "reply_to_top_id", None) or getattr(
                reply_header, "reply_to_msg_id", None
            )
        # Telegram's General topic has ID 1 and does not carry the forum flag.
        return 1

    def _cache_message_chat(self, msg_id: int, chat_id: int):
        if utils.resolve_id(chat_id)[1] is PeerChannel:
            return
        if len(self._msg_id_to_chat_id) > 15000:
            # Evict the oldest 5000 entries rather than clearing everything.
            for key in list(self._msg_id_to_chat_id.keys())[:5000]:
                del self._msg_id_to_chat_id[key]
        if (
            msg_id in self._msg_id_to_chat_id
            and self._msg_id_to_chat_id[msg_id] != chat_id
        ):
            self._msg_id_to_chat_id[msg_id] = None
        else:
            self._msg_id_to_chat_id[msg_id] = chat_id

    def _get_sender_color(self, sender: Any, sender_id: int) -> str:
        peer_color = getattr(sender, "color", None)
        color_index = getattr(peer_color, "color", None)
        if color_index is None:
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
                return sanitize_html(html.unparse(raw_text, entities or []))
            except Exception:
                return sanitize_html(html_escape(raw_text))
        return ""

    def _extract_reactions(self, reactions: Any) -> List[Reaction]:
        """Extract Reaction list from a Telethon MessageReactions object."""
        results = []
        if not reactions:
            return results

        reaction_counts = getattr(reactions, "results", [])
        recent_reactions = getattr(reactions, "recent_reactions", []) or []

        my_reaction_emojis: Set[str] = set()
        my_reaction_docs: Set[int] = set()

        if self.self_id and recent_reactions:
            for rr in recent_reactions:
                peer_id = None
                if isinstance(rr.peer_id, PeerUser):
                    peer_id = rr.peer_id.user_id
                elif isinstance(rr.peer_id, PeerChannel):
                    peer_id = rr.peer_id.channel_id
                elif isinstance(rr.peer_id, PeerChat):
                    peer_id = rr.peer_id.chat_id

                if peer_id == self.self_id:
                    if isinstance(rr.reaction, ReactionEmoji):
                        my_reaction_emojis.add(rr.reaction.emoticon)
                    elif isinstance(rr.reaction, ReactionCustomEmoji):
                        my_reaction_docs.add(rr.reaction.document_id)

        for rc in reaction_counts:
            emoji_str = ""
            custom_id = None
            is_chosen = getattr(rc, "chosen_order", None) is not None

            if isinstance(rc.reaction, ReactionEmoji):
                emoji_str = rc.reaction.emoticon
                if not is_chosen and emoji_str in my_reaction_emojis:
                    is_chosen = True

            elif isinstance(rc.reaction, ReactionCustomEmoji):
                custom_id = rc.reaction.document_id
                emoji_str = "⭐"
                if not is_chosen and custom_id in my_reaction_docs:
                    is_chosen = True

            if emoji_str or custom_id:
                results.append(
                    Reaction(
                        emoji=emoji_str,
                        count=rc.count,
                        is_chosen=is_chosen,
                        custom_emoji_id=custom_id,
                    )
                )
        return results

    async def _parse_message(
        self,
        msg: Any,
        replies_map: Dict[int, Any] | None = None,
        chat_id: Optional[int] = None,
        *,
        avatar_generation: Optional["AvatarGeneration"] = None,
        is_current: Optional[Callable[[], bool]] = None,
    ) -> Message:
        def check() -> None:
            raise_if_task_stopped()
            if is_current is not None and not is_current():
                raise AccountChanged()

        check()
        if avatar_generation is None:
            avatar_generation = self._media.avatar_generation()
        check()
        if chat_id:
            self._cache_message_chat(msg.id, chat_id)
        check()

        replies_map = replies_map or {}

        is_service = False
        text = ""

        if isinstance(msg, types.MessageService):
            is_service = True
            raw_text = get_message_action_text(msg) or "Service message"
            text = html_escape(raw_text)
        else:
            raw_text = getattr(msg, "message", "") or ""
            text = self._extract_text(msg)

        media = getattr(msg, "media", None)
        has_media = bool(media)
        is_video = False
        is_sticker = False
        sticker_emoji = None

        is_audio = False
        is_voice = False
        audio_title = None
        audio_performer = None
        audio_duration = None

        is_poll = False
        poll_question = None
        poll_options = []
        is_photo = isinstance(media, MessageMediaPhoto)
        media_width = media_height = None
        is_document = False
        file_name = None
        file_size = None
        webpage_url = None
        webpage_title = None
        webpage_description = None

        if has_media:
            if is_photo:
                sizes = getattr(getattr(media, "photo", None), "sizes", []) or []
                dimensions = []
                for size in sizes:
                    width, height = media_dimensions(size)
                    if width is not None and height is not None:
                        dimensions.append((width, height))
                if dimensions:
                    media_width, media_height = max(
                        dimensions, key=lambda size: size[0] * size[1]
                    )
            elif isinstance(media, MessageMediaPoll):
                is_poll = True
                poll = media.poll
                poll_question = getattr(poll.question, "text", "Poll")
                poll_options = [
                    getattr(answer.text, "text", "") for answer in poll.answers
                ]
            elif isinstance(media, MessageMediaWebPage):
                webpage = media.webpage
                url = getattr(webpage, "url", None)
                try:
                    parsed_url = urlsplit(url) if url else None
                    safe_url = (
                        parsed_url
                        and parsed_url.scheme in ("http", "https")
                        and parsed_url.hostname
                    )
                except ValueError:
                    safe_url = False
                if safe_url:
                    webpage_url = url
                    webpage_title = getattr(webpage, "title", None) or getattr(
                        webpage, "site_name", None
                    )
                    webpage_description = getattr(webpage, "description", None)
            elif isinstance(media, MessageMediaDocument):
                document = media.document
                file_size = getattr(document, "size", None)
                for attr in getattr(document, "attributes", []):
                    if isinstance(attr, DocumentAttributeVideo):
                        is_video = True
                        media_width, media_height = media_dimensions(attr)
                    elif isinstance(attr, DocumentAttributeSticker):
                        is_sticker = True
                        sticker_emoji = attr.alt
                    elif isinstance(attr, DocumentAttributeAudio):
                        is_audio = True
                        is_voice = getattr(attr, "voice", False)
                        audio_title = getattr(attr, "title", None)
                        audio_performer = getattr(attr, "performer", None)
                        audio_duration = getattr(attr, "duration", None)
                    elif isinstance(attr, DocumentAttributeFilename):
                        file_name = attr.file_name
                is_document = not (is_video or is_sticker or is_audio)

        forwarded_from = None
        forward = getattr(msg, "fwd_from", None)
        if forward:
            forwarded_from = getattr(forward, "from_name", None) or getattr(
                forward, "post_author", None
            )
            if not forwarded_from:
                forward_sender = getattr(getattr(msg, "forward", None), "sender", None)
                if forward_sender:
                    forwarded_from = utils.get_display_name(forward_sender)

        sender_name = getattr(msg, "post_author", None) or "Unknown"
        sender_id = 0
        sender_username = None
        avatar_url = None
        sender_initials = "?"
        sender_color = "#ccc"

        if getattr(msg, "from_id", None):
            sender_id = utils.get_peer_id(msg.from_id)

        sender = getattr(msg, "sender", None)
        if not sender and getattr(msg, "post", False):
            sender = getattr(msg, "chat", None)
        if sender_id and not sender:
            check()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + SENDER_LOOKUP_TIMEOUT

            async def resolve_sender() -> Any:
                check()
                return await self.client.get_entity(sender_id)

            try:
                resolved_sender = await run_with_task_deadline(
                    resolve_sender,
                    deadline,
                    name="message_sender_lookup",
                )
                check()
                sender = resolved_sender
            except AccountChanged:
                raise
            except TimeoutError:
                check()
                logger.warning("message_sender_lookup_timed_out", sender_id=sender_id)
            except Exception:
                check()
            finally:
                check()

        if sender:
            check()
            sender_id = utils.get_peer_id(sender)
            sender_name = utils.get_display_name(sender)
            sender_username = getattr(sender, "username", None)
            check()
            avatar_url = await self._media._get_chat_image(
                sender, sender_id, generation=avatar_generation
            )
            check()

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
                # Replies are plain text previews, not escaped markup.
                r_text = getattr(r_msg, "message", "") or ""
                if not r_text:
                    r_text = "📷 Media"
                reply_to_text = r_text
                r_sender = getattr(r_msg, "sender", None)
                reply_to_sender = (
                    utils.get_display_name(r_sender) if r_sender else "User"
                )

        reactions = self._extract_reactions(getattr(msg, "reactions", None))
        grouped_id = getattr(msg, "grouped_id", None)

        check()
        return Message(
            id=msg.id,
            text=text,
            date=msg.date or datetime.now(timezone.utc),
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
            is_photo=is_photo,
            is_video=is_video,
            media_width=media_width,
            media_height=media_height,
            is_sticker=is_sticker,
            sticker_emoji=sticker_emoji,
            is_document=is_document,
            file_name=file_name,
            file_size=file_size,
            webpage_url=webpage_url,
            webpage_title=webpage_title,
            webpage_description=webpage_description,
            forwarded_from=forwarded_from,
            is_audio=is_audio,
            is_voice=is_voice,
            audio_title=audio_title,
            audio_performer=audio_performer,
            audio_duration=audio_duration,
            is_poll=is_poll,
            poll_question=poll_question,
            poll_options=poll_options,
            is_service=is_service,
            reactions=reactions,
            grouped_id=grouped_id,
            raw_text=raw_text,
        )
