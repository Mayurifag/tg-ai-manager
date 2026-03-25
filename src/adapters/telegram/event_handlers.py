import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime
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


class EventHandlers:
    def __init__(
        self,
        client: Any,
        parser: "MessageParser",
        media: "MediaManager",
        get_topic_name_fn: Callable[[int, int], Awaitable[Optional[str]]],
    ) -> None:
        self.client = client
        self._parser = parser
        self._media = media
        self._get_topic_name_fn = get_topic_name_fn
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
            if event.chat_id:
                self._parser._cache_message_chat(event.message.id, event.chat_id)

            chat_name = "Unknown"
            try:
                chat = await event.get_chat()
                chat_name = utils.get_display_name(chat)
            except Exception:
                pass

            domain_msg = await self._parser._parse_message(
                event.message, chat_id=event.chat_id
            )
            topic_id = self._parser._extract_topic_id(event.message)
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
                link=f"/chat/{event.chat_id}",
                message_model=domain_msg,
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

            topic_id = self._parser._extract_topic_id(event.message)
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
                link=f"/chat/{event.chat_id}",
                message_model=domain_msg,
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
            chat_id = getattr(event, "chat_id", None)
            if not chat_id and event.deleted_ids:
                for did in event.deleted_ids:
                    if did in self._parser._msg_id_to_chat_id:
                        chat_id = self._parser._msg_id_to_chat_id[did]
                        break

            if not chat_id and hasattr(event, "original_update"):
                if hasattr(event.original_update, "channel_id"):
                    chat_id = utils.get_peer_id(
                        types.PeerChannel(event.original_update.channel_id)
                    )

            chat_name = "Unknown"
            if chat_id:
                try:
                    entity = await self.client.get_entity(chat_id)
                    chat_name = utils.get_display_name(entity)
                except Exception:
                    chat_name = f"Chat {chat_id}"

            if chat_id and event.deleted_ids:
                sys_event = SystemEvent(
                    type="deleted",
                    text="Message deleted",
                    chat_name=chat_name,
                    topic_name=None,
                    chat_id=chat_id,
                    link=f"/chat/{chat_id}" if chat_id else "#",
                    message_model=Message(
                        id=event.deleted_ids[0],
                        text="",
                        date=datetime.now(),
                        sender_name="",
                        is_outgoing=False,
                    ),
                )
                await self._dispatch(sys_event)

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

            chat_name = "Unknown"
            try:
                chat = await event.get_chat()
                chat_name = utils.get_display_name(chat)
            except Exception:
                pass

            text = "Unknown action"
            action_text = get_message_action_text(event.action_message)
            if action_text:
                msg_model = await self._parser._parse_message(
                    event.action_message, chat_id=event.chat_id
                )
                text = f"{msg_model.sender_name} {action_text}"

            sys_event = SystemEvent(
                type="action",
                text=text,
                chat_name=chat_name,
                topic_name=None,
                chat_id=event.chat_id,
                link=f"/chat/{event.chat_id}",
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
        link = f"/chat/{chat_id}"
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
