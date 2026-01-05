import asyncio
import traceback
from typing import Callable, Awaitable, List, Optional, Any, Dict
from datetime import datetime
from telethon import events, utils, types
from telethon.tl.types import MessageActionChatEditPhoto, MessageActionChatDeletePhoto
from src.domain.models import SystemEvent, Message
from src.adapters.telethon_mappers import get_message_action_text
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class EventHandlersMixin:
    def __init__(self):
        self.listeners: List[Callable[[SystemEvent], Awaitable[None]]] = []
        self._msg_id_to_chat_id: Dict[int, int] = {}
        self.client: Any = None

    # Abstract deps
    async def _parse_message(
        self,
        msg: Any,
        replies_map: Dict[int, Any] | None = None,
        chat_id: Optional[int] = None,
    ) -> Message:
        raise NotImplementedError

    def _extract_topic_id(self, message: Any) -> Optional[int]:
        raise NotImplementedError

    async def get_topic_name(self, chat_id: int, topic_id: int) -> Optional[str]:
        raise NotImplementedError

    def _cache_message_chat(self, msg_id: int, chat_id: int):
        raise NotImplementedError

    def clear_chat_avatar(self, chat_id: int):
        raise NotImplementedError

    def add_event_listener(self, callback: Callable[[SystemEvent], Awaitable[None]]):
        self.listeners.append(callback)

    async def _dispatch(self, event: SystemEvent):
        for listener in self.listeners:
            try:
                await listener(event)
            except Exception as e:
                logger.error(
                    "event_listener_error",
                    error=repr(e),
                    traceback=traceback.format_exc(),
                )

    async def _handle_new_message(self, event):
        try:
            if event.chat_id:
                self._cache_message_chat(event.message.id, event.chat_id)

            chat_name = "Unknown"
            try:
                chat = await event.get_chat()
                chat_name = utils.get_display_name(chat)
            except:
                pass

            domain_msg = await self._parse_message(event.message, chat_id=event.chat_id)
            topic_id = self._extract_topic_id(event.message)
            topic_name = None
            if topic_id:
                topic_name = await self.get_topic_name(event.chat_id, topic_id)

            display_chat_name = chat_name
            if topic_name:
                display_chat_name = f"{topic_name} - {chat_name}"

            # DRY: Use shared model method, no truncation here (frontend handles it)
            preview = domain_msg.get_preview_text()

            sys_event = SystemEvent(
                type="message",
                text=preview,
                chat_name=display_chat_name,
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

    async def _handle_edited_message(self, event):
        try:
            if event.chat_id:
                self._cache_message_chat(event.message.id, event.chat_id)

            chat_name = "Unknown"
            try:
                chat = await event.get_chat()
                chat_name = utils.get_display_name(chat)
            except:
                pass

            domain_msg = await self._parse_message(event.message, chat_id=event.chat_id)

            # DRY: Use shared model method, no truncation here
            preview = domain_msg.get_preview_text()

            topic_id = self._extract_topic_id(event.message)
            topic_name = None
            if topic_id:
                topic_name = await self.get_topic_name(event.chat_id, topic_id)

            display_chat_name = chat_name
            if topic_name:
                display_chat_name = f"{topic_name} - {chat_name}"

            sys_event = SystemEvent(
                type="edited",
                text=preview,
                chat_name=display_chat_name,
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

    async def _handle_deleted_message(self, event):
        try:
            chat_id = getattr(event, "chat_id", None)
            if not chat_id and event.deleted_ids:
                for did in event.deleted_ids:
                    if did in self._msg_id_to_chat_id:
                        chat_id = self._msg_id_to_chat_id[did]
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
                except:
                    chat_name = f"Chat {chat_id}"
            else:
                logger.debug(
                    "unresolved_chat_id_for_delete", deleted_ids=event.deleted_ids
                )

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

    async def _handle_chat_action(self, event):
        try:
            # Handle Cache Invalidation for Avatar
            action = getattr(event.action_message, "action", None)
            if isinstance(
                action, (MessageActionChatEditPhoto, MessageActionChatDeletePhoto)
            ):
                if event.chat_id:
                    self.clear_chat_avatar(event.chat_id)

            chat_name = "Unknown"
            try:
                chat = await event.get_chat()
                chat_name = utils.get_display_name(chat)
            except:
                pass

            text = "Unknown action"
            action_text = get_message_action_text(event.action_message)
            if action_text:
                msg_model = await self._parse_message(
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
