from datetime import datetime
from typing import Any, Optional

from telethon import events, utils
from telethon.tl.types import (
    MessageActionChatDeletePhoto,
    MessageActionChatEditPhoto,
    PeerChannel,
    PeerChat,
    PeerUser,
    UpdateMessageReactions,
)

from src.adapters.telethon_mappers import get_message_action_text
from src.domain.models import Message, SystemEvent
from src.infrastructure.event_bus import EventBus
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class TelegramEventListener:
    def __init__(
        self, client: Any, event_bus: EventBus, parser: Any, media: Any, reader: Any
    ):
        self.client = client
        self.event_bus = event_bus
        self.parser = parser
        self.media = media
        self.reader = reader
        self._registered = False

    def register_handlers(self):
        if not self._registered:
            self.client.add_event_handler(self._handle_new_message, events.NewMessage())
            self.client.add_event_handler(
                self._handle_edited_message, events.MessageEdited()
            )
            self.client.add_event_handler(
                self._handle_deleted_message, events.MessageDeleted()
            )
            self.client.add_event_handler(self._handle_chat_action, events.ChatAction())
            self.client.add_event_handler(self._handle_raw_update)
            self._registered = True

    def _extract_topic_id(self, message: Any) -> Optional[int]:
        reply_header = getattr(message, "reply_to", None)
        if reply_header:
            return getattr(reply_header, "reply_to_top_id", None)
        return None

    async def _handle_new_message(self, event):
        try:
            if event.chat_id:
                self.reader.cache_message_chat(event.message.id, event.chat_id)

            chat_name = "Unknown"
            try:
                chat = await event.get_chat()
                chat_name = utils.get_display_name(chat)
            except Exception:
                pass

            domain_msg = await self.parser.parse(event.message, chat_id=event.chat_id)
            topic_id = self._extract_topic_id(event.message)
            topic_name = None
            if topic_id:
                topic_name = await self.reader.get_topic_name(event.chat_id, topic_id)

            await self.event_bus.publish(
                SystemEvent(
                    type="message",
                    text=domain_msg.get_preview_text(),
                    chat_name=chat_name,
                    topic_name=topic_name,
                    chat_id=event.chat_id,
                    topic_id=topic_id,
                    link=f"/chat/{event.chat_id}",
                    message_model=domain_msg,
                )
            )
        except Exception as e:
            logger.error("handle_new_message_error", error=repr(e))

    async def _handle_edited_message(self, event):
        try:
            if event.chat_id:
                self.reader.cache_message_chat(event.message.id, event.chat_id)

            domain_msg = await self.parser.parse(event.message, chat_id=event.chat_id)
            topic_id = self._extract_topic_id(event.message)

            await self.event_bus.publish(
                SystemEvent(
                    type="edited",
                    text=domain_msg.get_preview_text(),
                    chat_name="",
                    chat_id=event.chat_id,
                    topic_id=topic_id,
                    link=f"/chat/{event.chat_id}",
                    message_model=domain_msg,
                )
            )
        except Exception:
            pass

    async def _handle_deleted_message(self, event):
        try:
            chat_id = getattr(event, "chat_id", None)
            # Try to resolve chat_id from internal cache if missing
            if not chat_id and event.deleted_ids:
                for did in event.deleted_ids:
                    found = self.reader.get_chat_id_by_msg(did)
                    if found:
                        chat_id = found
                        break

            if not chat_id and hasattr(event, "original_update"):
                if hasattr(event.original_update, "channel_id"):
                    chat_id = utils.get_peer_id(
                        PeerChannel(event.original_update.channel_id)
                    )

            if chat_id and event.deleted_ids:
                await self.event_bus.publish(
                    SystemEvent(
                        type="deleted",
                        text="Message deleted",
                        chat_name="",
                        chat_id=chat_id,
                        link=f"/chat/{chat_id}",
                        message_model=Message(
                            id=event.deleted_ids[0],
                            text="",
                            date=datetime.now(),
                            sender_name="",
                            is_outgoing=False,
                        ),
                    )
                )
        except Exception as e:
            logger.error("handle_deleted_error", error=str(e))

    async def _handle_chat_action(self, event):
        try:
            action = getattr(event.action_message, "action", None)
            if isinstance(
                action, (MessageActionChatEditPhoto, MessageActionChatDeletePhoto)
            ):
                if event.chat_id:
                    self.media.clear_avatar_cache(event.chat_id)

            text = get_message_action_text(event.action_message) or "Action"
            chat_name = "Unknown"
            try:
                c = await event.get_chat()
                chat_name = utils.get_display_name(c)
            except Exception:
                pass

            await self.event_bus.publish(
                SystemEvent(
                    type="action",
                    text=text,
                    chat_name=chat_name,
                    chat_id=event.chat_id,
                    link=f"/chat/{event.chat_id}",
                )
            )
        except Exception:
            pass

    async def _handle_raw_update(self, event):
        if isinstance(event, UpdateMessageReactions):
            try:
                chat_id = 0
                if isinstance(event.peer, PeerUser):
                    chat_id = event.peer.user_id
                elif isinstance(event.peer, PeerChannel):
                    chat_id = utils.get_peer_id(event.peer)
                elif isinstance(event.peer, PeerChat):
                    chat_id = utils.get_peer_id(event.peer)

                if not chat_id:
                    return

                # Mock parse just to get reactions
                class MockMsg:
                    def __init__(self, r):
                        self.reactions = r

                mock_msg = MockMsg(event.reactions)
                reactions_list = self.parser._extract_reactions(mock_msg)

                await self.event_bus.publish(
                    SystemEvent(
                        type="reaction_update",
                        text="",
                        chat_name="",
                        chat_id=chat_id,
                        link=f"/chat/{chat_id}",
                        message_model=Message(
                            id=event.msg_id,
                            text="",
                            date=datetime.now(),
                            sender_name="",
                            is_outgoing=False,
                            reactions=reactions_list,
                        ),
                    )
                )
            except Exception as e:
                logger.error("raw_update_error", error=str(e))
