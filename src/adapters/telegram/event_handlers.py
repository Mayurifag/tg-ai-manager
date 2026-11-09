import asyncio
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
from src.infrastructure.tasks import raise_if_task_stopped, run_with_task_deadline

if TYPE_CHECKING:
    from src.adapters.telegram.media import MediaManager
    from src.adapters.telegram.message_parser import MessageParser

logger = get_logger(__name__)
# shortcut: event identity/name lookups get 10 seconds; revisit if Telegram
# regularly takes longer. Unknown chat scope cannot authorize automation.
EVENT_NAME_LOOKUP_TIMEOUT = 10.0


def message_link(
    chat_id: Optional[int],
    topic_id: Optional[int] = None,
) -> Optional[str]:
    if not chat_id:
        return None
    link = f"/chat/{chat_id}"
    if topic_id is not None:
        link += f"/topic/{topic_id}"
    return link


class EventHandlers:
    def __init__(
        self,
        client: Any,
        parser: "MessageParser",
        media: "MediaManager",
        get_topic_name_fn: Callable[[int, int], Awaitable[Optional[str]]],
        invalidate_topic_name_fn: Optional[Callable[[int, int], None]] = None,
        *,
        get_topic_name_before_fn: Optional[
            Callable[[int, int, float], Awaitable[Optional[str]]]
        ] = None,
    ) -> None:
        self.client = client
        self._parser = parser
        self._media = media
        self._get_topic_name_fn = get_topic_name_fn
        self._get_topic_name_before_fn = get_topic_name_before_fn
        self._invalidate_topic_name_fn = invalidate_topic_name_fn
        self.listeners: List[Callable[[SystemEvent], Awaitable[None]]] = []

    def add_event_listener(
        self, callback: Callable[[SystemEvent], Awaitable[None]]
    ) -> None:
        self.listeners.append(callback)

    async def _event_chat(self, event: Any) -> tuple[Any, str]:
        raise_if_task_stopped()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + EVENT_NAME_LOOKUP_TIMEOUT
        try:
            chat = await run_with_task_deadline(
                event.get_chat, deadline, name="event_chat_lookup"
            )
        except TimeoutError:
            raise_if_task_stopped()
            logger.warning("event_chat_lookup_timed_out", chat_id=event.chat_id)
            return None, "Unknown"
        except Exception:
            raise_if_task_stopped()
            return None, "Unknown"
        finally:
            raise_if_task_stopped()
        try:
            name = utils.get_display_name(chat) or "Unknown"
        except Exception:
            name = "Unknown"
        return chat, name

    async def _topic_name(self, chat_id: int, topic_id: int) -> Optional[str]:
        raise_if_task_stopped()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + EVENT_NAME_LOOKUP_TIMEOUT

        async def lookup() -> Optional[str]:
            if self._get_topic_name_before_fn:
                return await self._get_topic_name_before_fn(chat_id, topic_id, deadline)
            return await self._get_topic_name_fn(chat_id, topic_id)

        try:
            return await run_with_task_deadline(
                lookup, deadline, name="event_topic_name"
            )
        except Exception as error:
            raise_if_task_stopped()
            logger.warning(
                "event_topic_name_failed",
                chat_id=chat_id,
                topic_id=topic_id,
                category=type(error).__name__,
            )
            return None
        finally:
            raise_if_task_stopped()

    async def _chat_name(
        self, chat_id: int, fallback: str, *, deadline: Optional[float] = None
    ) -> str:
        raise_if_task_stopped()
        loop = asyncio.get_running_loop()
        if deadline is None:
            deadline = loop.time() + EVENT_NAME_LOOKUP_TIMEOUT
        if deadline <= loop.time():
            return fallback

        async def lookup() -> str:
            entity = await self.client.get_entity(chat_id)
            raise_if_task_stopped()
            return utils.get_display_name(entity) or fallback

        try:
            return await run_with_task_deadline(
                lookup, deadline, name="event_chat_name"
            )
        except Exception as error:
            raise_if_task_stopped()
            logger.warning(
                "event_chat_name_failed",
                chat_id=chat_id,
                category=type(error).__name__,
            )
            return fallback
        finally:
            raise_if_task_stopped()

    async def _dispatch(self, event: SystemEvent) -> None:
        async def deliver() -> None:
            raise_if_task_stopped()
            for listener in self.listeners:
                raise_if_task_stopped()
                try:
                    await listener(event)
                except Exception as e:
                    raise_if_task_stopped()
                    logger.error(
                        "event_listener_error",
                        listener=getattr(
                            listener, "__qualname__", type(listener).__name__
                        ),
                        category=type(e).__name__,
                    )
                finally:
                    raise_if_task_stopped()

        await run_with_task_deadline(deliver, None, name="telegram_event_dispatch")

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
            avatar_generation = self._media.avatar_generation()
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

            chat, chat_name = await self._event_chat(event)

            domain_msg = await self._parser._parse_message(
                event.message,
                chat_id=event.chat_id,
                avatar_generation=avatar_generation,
            )
            topic_id = self._extract_forum_topic_id(chat, event.message)
            self._invalidate_edited_topic(event.chat_id, topic_id, event.message)
            topic_name = None
            if topic_id:
                topic_name = await self._topic_name(event.chat_id, topic_id)

            preview = domain_msg.get_preview_text()

            sys_event = SystemEvent(
                type="message",
                text=preview,
                chat_name=chat_name,
                topic_name=topic_name,
                chat_id=event.chat_id,
                topic_id=topic_id,
                link=message_link(event.chat_id, topic_id),
                message_model=domain_msg,
                scope_known=chat is not None
                and (not getattr(chat, "forum", False) or topic_id is not None),
            )
            await self._dispatch(sys_event)
        except Exception as e:
            logger.error(
                "handle_new_message_error",
                category=type(e).__name__,
            )

    async def _handle_edited_message(self, event: Any) -> None:
        try:
            avatar_generation = self._media.avatar_generation()
            if event.chat_id:
                self._parser._cache_message_chat(event.message.id, event.chat_id)
                self._media.invalidate_message(event.chat_id, event.message.id)
                self._media.remember_messages(event.chat_id, [event.message])

            chat, chat_name = await self._event_chat(event)

            domain_msg = await self._parser._parse_message(
                event.message,
                chat_id=event.chat_id,
                avatar_generation=avatar_generation,
            )
            preview = domain_msg.get_preview_text()

            topic_id = self._extract_forum_topic_id(chat, event.message)
            self._invalidate_edited_topic(event.chat_id, topic_id, event.message)
            topic_name = None
            if topic_id:
                topic_name = await self._topic_name(event.chat_id, topic_id)

            sys_event = SystemEvent(
                type="edited",
                text=preview,
                chat_name=chat_name,
                topic_name=topic_name,
                chat_id=event.chat_id,
                topic_id=topic_id,
                link=message_link(event.chat_id, topic_id),
                message_model=domain_msg,
                scope_known=chat is not None
                and (not getattr(chat, "forum", False) or topic_id is not None),
            )
            await self._dispatch(sys_event)
        except Exception as e:
            logger.error(
                "handle_edited_message_error",
                category=type(e).__name__,
            )

    async def _handle_deleted_message(self, event: Any) -> None:
        try:
            known_chat = getattr(event, "chat_id", None)
            original = getattr(event, "original_update", None)
            channel_id = getattr(original, "channel_id", None)
            if channel_id:
                known_chat = utils.get_peer_id(types.PeerChannel(channel_id))
            resolved: list[tuple[int, int]] = []
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
                resolved.append((chat_id, message_id))

            if unknown:
                # A resync of the views alone would leave deleted media in the
                # separate preview/reference caches, including in-flight work.
                self._media.invalidate_all_messages()
                await self._dispatch(
                    SystemEvent(
                        type="resync_required",
                        text="Message identity unavailable",
                        chat_name="",
                    )
                )

            # Invalidate the whole update before optional lookups or dispatch
            # can suspend, so one slow chat cannot leave later media stale.
            names: dict[int, str] = {}
            deadline = asyncio.get_running_loop().time() + EVENT_NAME_LOOKUP_TIMEOUT
            for chat_id, message_id in resolved:
                if chat_id not in names:
                    names[chat_id] = await self._chat_name(
                        chat_id, f"Chat {chat_id}", deadline=deadline
                    )
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
                            date=datetime.now(timezone.utc),
                            sender_name="",
                            is_outgoing=False,
                        ),
                    )
                )
        except Exception as e:
            logger.error(
                "handle_deleted_message_error",
                category=type(e).__name__,
            )

    async def _handle_chat_action(self, event: Any) -> None:
        try:
            action = getattr(event.action_message, "action", None)
            if isinstance(
                action, (MessageActionChatEditPhoto, MessageActionChatDeletePhoto)
            ):
                if event.chat_id:
                    self._media.clear_chat_avatar(event.chat_id)

            avatar_generation = self._media.avatar_generation()
            chat, chat_name = await self._event_chat(event)

            msg_model = None
            topic_id = None
            text = "Unknown action"
            action_text = get_message_action_text(event.action_message)
            if action_text or isinstance(
                action, (types.MessageActionTopicCreate, types.MessageActionTopicEdit)
            ):
                msg_model = await self._parser._parse_message(
                    event.action_message,
                    chat_id=event.chat_id,
                    avatar_generation=avatar_generation,
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
                link=message_link(event.chat_id, topic_id),
                message_model=msg_model,
                scope_known=chat is not None
                and (not getattr(chat, "forum", False) or topic_id is not None),
            )
            await self._dispatch(sys_event)
        except Exception as e:
            logger.error(
                "handle_chat_action_error",
                category=type(e).__name__,
            )

    async def _handle_other_updates(self, event: Any) -> None:
        """Capture profile changes, reactions and remote inbox reads."""
        try:
            if isinstance(event, UpdateMessageReactions):
                await self._process_reaction_update(event)
                return
            if isinstance(event, (types.UpdateUserName, types.UpdateUser)):
                profile_id = utils.get_peer_id(types.PeerUser(event.user_id))
            elif isinstance(event, types.UpdateChannel):
                profile_id = utils.get_peer_id(types.PeerChannel(event.channel_id))
            elif isinstance(event, types.UpdateChat):
                profile_id = utils.get_peer_id(types.PeerChat(event.chat_id))
            else:
                profile_id = None
            if profile_id is not None:
                if not isinstance(event, types.UpdateUserName):
                    entity = getattr(event, "_entities", {}).get(profile_id)
                    if entity is not None and not getattr(entity, "min", False):
                        await self._media._get_chat_image(entity, profile_id)
                    else:
                        self._media.clear_chat_avatar(profile_id)
                await self._dispatch(
                    SystemEvent(
                        type="profile_updated",
                        text="Profile changed",
                        chat_name="",
                        chat_id=profile_id,
                    )
                )
                return
            if isinstance(event, types.UpdateReadHistoryInbox):
                chat_id = utils.get_peer_id(event.peer)
                topic_id = event.top_msg_id
                read_through = event.max_id
            elif isinstance(event, types.UpdateReadChannelInbox):
                chat_id = utils.get_peer_id(types.PeerChannel(event.channel_id))
                topic_id = None
                read_through = event.max_id
            elif isinstance(event, types.UpdateReadChannelDiscussionInbox):
                chat_id = utils.get_peer_id(types.PeerChannel(event.channel_id))
                topic_id = event.top_msg_id
                read_through = event.read_max_id
            else:
                return
            await self._dispatch(
                SystemEvent(
                    type="read",
                    text="Read state changed",
                    chat_name="",
                    chat_id=chat_id,
                    topic_id=topic_id,
                    link=message_link(chat_id, topic_id),
                    read_origin="remote",
                    read_through=read_through,
                )
            )

        except Exception as e:
            logger.error(
                "handle_other_updates_error",
                category=type(e).__name__,
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

        link = message_link(chat_id)
        chat_name = await self._chat_name(chat_id, "Unknown")

        reactions_list: List[Reaction] = self._parser._extract_reactions(
            event.reactions
        )

        msg_model = Message(
            id=event.msg_id,
            text="",
            date=datetime.now(timezone.utc),
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
