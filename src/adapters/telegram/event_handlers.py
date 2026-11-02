import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from html import escape as html_escape
from typing import TYPE_CHECKING, Any, List, Optional

from telethon import types, utils
from telethon.tl.types import (
    MessageActionChatDeletePhoto,
    MessageActionChatEditPhoto,
    PeerChannel,
    PeerChat,
    PeerUser,
    UpdateMessageReactions,
)

from src.adapters.telethon_mappers import get_message_action_text
from src.domain.models import Message, Reaction, SystemEvent
from src.infrastructure.logging import get_logger

if TYPE_CHECKING:
    from src.adapters.telegram.media import MediaManager
    from src.adapters.telegram.message_parser import MessageParser

logger = get_logger(__name__)


def message_link(
    chat_id: Optional[int],
    topic_id: Optional[int] = None,
    message_id: Optional[int] = None,
) -> Optional[str]:
    if not chat_id:
        return None
    link = f"/chat/{chat_id}"
    if topic_id is not None:
        link += f"/topic/{topic_id}"
    if message_id is not None:
        link += f"#msg-{message_id}"
    return link


class EventHandlers:
    def __init__(
        self,
        client: Any,
        parser: "MessageParser",
        media: "MediaManager",
        get_topic_name_fn: Callable[[int, int], Awaitable[Optional[str]]],
        invalidate_topic_name_fn: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        self.client = client
        self._parser = parser
        self._media = media
        self._get_topic_name_fn = get_topic_name_fn
        self._invalidate_topic_name_fn = invalidate_topic_name_fn
        self.listeners: List[Callable[[SystemEvent], Awaitable[None]]] = []

    def add_event_listener(
        self, callback: Callable[[SystemEvent], Awaitable[None]]
    ) -> None:
        self.listeners.append(callback)

    async def _dispatch(self, event: SystemEvent) -> None:
        for listener in self.listeners:
            try:
                await listener(event)
            except Exception as e:
                logger.error(
                    "event_listener_error",
                    error=repr(e),
                    traceback=traceback.format_exc(),
                )

    def _extract_forum_topic_id(self, chat: Any, message: Any) -> Optional[int]:
        if not getattr(chat, "forum", False):
            return None
        return self._parser._extract_topic_id(message)

    def _invalidate_edited_topic(
        self, chat_id: int, topic_id: Optional[int], message: Any
    ) -> None:
        if (
            topic_id is not None
            and self._invalidate_topic_name_fn
            and isinstance(
                getattr(message, "action", None),
                (types.MessageActionTopicCreate, types.MessageActionTopicEdit),
            )
        ):
            self._invalidate_topic_name_fn(chat_id, topic_id)

    def register_handlers(self, client: Any) -> None:
        """Register all Telethon event handlers on the given client."""
        from telethon import events

        client.add_event_handler(self._handle_new_message, events.NewMessage())
        client.add_event_handler(self._handle_edited_message, events.MessageEdited())
        client.add_event_handler(self._handle_deleted_message, events.MessageDeleted())
        client.add_event_handler(self._handle_chat_action, events.ChatAction())
        client.add_event_handler(self._handle_other_updates)

    async def _handle_new_message(self, event: Any) -> None:
        try:
            message_date = getattr(event.message, "date", None)
            if isinstance(message_date, datetime):
                logger.info(
                    "telegram_message_received",
                    chat_id=event.chat_id,
                    message_id=event.message.id,
                    message_date=message_date.isoformat(),
                    delivery_seconds=round(
                        (
                            datetime.now(timezone.utc)
                            - message_date.replace(
                                tzinfo=message_date.tzinfo or timezone.utc
                            )
                        ).total_seconds(),
                        3,
                    ),
                )
            if event.chat_id:
                self._parser._cache_message_chat(event.message.id, event.chat_id)

            chat = None
            chat_name = "Unknown"
            try:
                chat = await event.get_chat()
                chat_name = utils.get_display_name(chat)
            except Exception:
                pass

            domain_msg = await self._parser._parse_message(
                event.message, chat_id=event.chat_id
            )
            topic_id = self._extract_forum_topic_id(chat, event.message)
            self._invalidate_edited_topic(event.chat_id, topic_id, event.message)
            topic_name = None
            if topic_id:
                topic_name = await self._get_topic_name_fn(event.chat_id, topic_id)

            preview = domain_msg.get_preview_text()

            sys_event = SystemEvent(
                type="message",
                text=preview,
                chat_name=chat_name,
                topic_name=topic_name,
                chat_id=event.chat_id,
                topic_id=topic_id,
                link=message_link(event.chat_id, topic_id, domain_msg.id),
                message_model=domain_msg,
                scope_known=chat is not None
                and (not getattr(chat, "forum", False) or topic_id is not None),
            )
            await self._dispatch(sys_event)
        except Exception as e:
            logger.error(
                "handle_new_message_error",
                error=repr(e),
                traceback=traceback.format_exc(),
            )

    async def _handle_edited_message(self, event: Any) -> None:
        try:
            if event.chat_id:
                self._parser._cache_message_chat(event.message.id, event.chat_id)
                self._media.invalidate_message(event.chat_id, event.message.id)
                self._media.remember_messages(event.chat_id, [event.message])

            chat = None
            chat_name = "Unknown"
            try:
                chat = await event.get_chat()
                chat_name = utils.get_display_name(chat)
            except Exception:
                pass

            domain_msg = await self._parser._parse_message(
                event.message, chat_id=event.chat_id
            )
            preview = domain_msg.get_preview_text()

            topic_id = self._extract_forum_topic_id(chat, event.message)
            self._invalidate_edited_topic(event.chat_id, topic_id, event.message)
            topic_name = None
            if topic_id:
                topic_name = await self._get_topic_name_fn(event.chat_id, topic_id)

            sys_event = SystemEvent(
                type="edited",
                text=preview,
                chat_name=chat_name,
                topic_name=topic_name,
                chat_id=event.chat_id,
                topic_id=topic_id,
                link=message_link(event.chat_id, topic_id, domain_msg.id),
                message_model=domain_msg,
                scope_known=chat is not None
                and (not getattr(chat, "forum", False) or topic_id is not None),
            )
            await self._dispatch(sys_event)
        except Exception as e:
            logger.error(
                "handle_edited_message_error",
                error=repr(e),
                traceback=traceback.format_exc(),
            )

    async def _handle_deleted_message(self, event: Any) -> None:
        try:
            known_chat = getattr(event, "chat_id", None)
            original = getattr(event, "original_update", None)
            channel_id = getattr(original, "channel_id", None)
            if channel_id:
                known_chat = utils.get_peer_id(types.PeerChannel(channel_id))
            names: dict[int, str] = {}
            unknown = False
            for message_id in event.deleted_ids or []:
                chat_id = known_chat or self._parser._msg_id_to_chat_id.get(message_id)
                if not chat_id:
                    unknown = True
                    continue
                self._media.invalidate_message(chat_id, message_id)
                if self._invalidate_topic_name_fn:
                    self._invalidate_topic_name_fn(chat_id, message_id)
                if not known_chat:
                    self._parser._msg_id_to_chat_id.pop(message_id, None)
                if chat_id not in names:
                    try:
                        entity = await self.client.get_entity(chat_id)
                        names[chat_id] = (
                            utils.get_display_name(entity) or f"Chat {chat_id}"
                        )
                    except Exception:
                        names[chat_id] = f"Chat {chat_id}"
                await self._dispatch(
                    SystemEvent(
                        type="deleted",
                        text="Message deleted",
                        chat_name=names[chat_id],
                        topic_name=None,
                        chat_id=chat_id,
                        link=message_link(chat_id),
                        message_model=Message(
                            id=message_id,
                            text="",
                            date=datetime.now(),
                            sender_name="",
                            is_outgoing=False,
                        ),
                    )
                )
            if unknown:
                await self._dispatch(
                    SystemEvent(
                        type="resync_required",
                        text="Message identity unavailable",
                        chat_name="",
                    )
                )

        except Exception as e:
            logger.error(
                "handle_deleted_message_error",
                error=repr(e),
                traceback=traceback.format_exc(),
            )

    async def _handle_chat_action(self, event: Any) -> None:
        try:
            action = getattr(event.action_message, "action", None)
            if isinstance(
                action, (MessageActionChatEditPhoto, MessageActionChatDeletePhoto)
            ):
                if event.chat_id:
                    self._media.clear_chat_avatar(event.chat_id)

            chat = None
            chat_name = "Unknown"
            try:
                chat = await event.get_chat()
                chat_name = utils.get_display_name(chat)
            except Exception:
                pass

            msg_model = None
            topic_id = None
            text = "Unknown action"
            action_text = get_message_action_text(event.action_message)
            if action_text or isinstance(
                action, (types.MessageActionTopicCreate, types.MessageActionTopicEdit)
            ):
                msg_model = await self._parser._parse_message(
                    event.action_message, chat_id=event.chat_id
                )
                topic_id = self._extract_forum_topic_id(chat, event.action_message)
                self._invalidate_edited_topic(
                    event.chat_id, topic_id, event.action_message
                )
                text = html_escape(
                    f"{msg_model.sender_name} {action_text or 'updated a topic'}"
                )

            sys_event = SystemEvent(
                type="action",
                text=text,
                chat_name=chat_name,
                topic_name=None,
                chat_id=event.chat_id,
                topic_id=topic_id,
                link=message_link(
                    event.chat_id, topic_id, msg_model.id if msg_model else None
                ),
                message_model=msg_model,
                scope_known=chat is not None
                and (not getattr(chat, "forum", False) or topic_id is not None),
            )
            await self._dispatch(sys_event)
        except Exception as e:
            logger.error(
                "handle_chat_action_error",
                error=repr(e),
                traceback=traceback.format_exc(),
            )

    async def _handle_other_updates(self, event: Any) -> None:
        """Captures other relevant updates like reactions."""
        try:
            if isinstance(event, UpdateMessageReactions):
                await self._process_reaction_update(event)
                return

        except Exception as e:
            logger.error(
                "handle_other_updates_error",
                error=repr(e),
                traceback=traceback.format_exc(),
            )

    async def _process_reaction_update(self, event: Any) -> None:
        chat_id = 0
        if isinstance(event.peer, PeerUser):
            chat_id = event.peer.user_id
        elif isinstance(event.peer, PeerChannel):
            chat_id = utils.get_peer_id(event.peer)
        elif isinstance(event.peer, PeerChat):
            chat_id = utils.get_peer_id(event.peer)

        if not chat_id:
            return

        chat_name = "Unknown"
        link = message_link(chat_id, message_id=event.msg_id)
        try:
            chat_entity = await self.client.get_entity(chat_id)
            chat_name = utils.get_display_name(chat_entity)
        except Exception:
            pass

        reactions_list: List[Reaction] = self._parser._extract_reactions(
            event.reactions
        )

        msg_model = Message(
            id=event.msg_id,
            text="",
            date=datetime.now(),
            sender_name="",
            is_outgoing=False,
            reactions=reactions_list,
        )

        sys_event = SystemEvent(
            type="reaction_update",
            text="Reaction updated",
            chat_name=chat_name,
            chat_id=chat_id,
            link=link,
            message_model=msg_model,
        )
        await self._dispatch(sys_event)
