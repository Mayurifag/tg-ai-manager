import asyncio
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from telethon import errors, types, utils
from telethon.tl.functions.messages import GetPeerDialogsRequest
from telethon.tl.types import InputDialogPeer

from src.adapters.telethon_mappers import (
    format_message_preview,
    map_telethon_dialog_to_chat_type,
)
from src.adapters.telegram.query_errors import unavailable
from src.domain.models import Chat, ChatType, Message, MessageContext
from src.infrastructure.logging import get_logger

if TYPE_CHECKING:
    from src.adapters.telegram.media import MediaManager
    from src.adapters.telegram.message_parser import MessageParser

logger = get_logger(__name__)

# Loading the complete list must not evict the live message-to-chat cache.
RECENT_DIALOG_CACHE_LIMIT = 20


class ChatQueryOps:
    def __init__(
        self,
        client: Any,
        parser: "MessageParser",
        media: "MediaManager",
    ) -> None:
        self.client = client
        self._parser = parser
        self._media = media
        self._unavailable_chats: Dict[int, float] = {}

    async def get_chats(self) -> list[Chat]:
        try:
            dialogs = await self.client.get_dialogs(limit=None)
        except Exception as error:
            logger.warning("get_chats_failed", category=type(error).__name__)
            raise unavailable(error) from error
        results = []
        for d in dialogs:
            chat_type = map_telethon_dialog_to_chat_type(d)
            unread_count = d.unread_count
            unread_topics_count = None

            image_url = await self._media._get_chat_image(d.entity, d.id)

            msg = getattr(d, "message", None)
            if msg and len(results) < RECENT_DIALOG_CACHE_LIMIT:
                self._parser._cache_message_chat(msg.id, d.id)

            preview = format_message_preview(msg, chat_type, {})

            results.append(
                Chat(
                    id=d.id,
                    name=d.name,
                    unread_count=unread_count,
                    type=chat_type,
                    unread_topics_count=unread_topics_count,
                    last_message_preview=preview,
                    image_url=image_url,
                    is_pinned=d.pinned,
                )
            )
        return results

    async def get_all_unread_chats(
        self, chat_ids: Optional[List[int]] = None
    ) -> List[Chat]:
        if chat_ids is not None:
            return await self._get_unread_chats_by_id(chat_ids)
        results = []
        try:
            folders = [0, 1]
            for folder_id in folders:
                count = 0
                async for d in self.client.iter_dialogs(
                    limit=None, ignore_migrated=True, folder=folder_id
                ):
                    count += 1
                    if count % 50 == 0:
                        await asyncio.sleep(0)

                    if d.unread_count > 0 or d.unread_mentions_count > 0:
                        chat_type = map_telethon_dialog_to_chat_type(d)
                        chat = Chat(
                            id=d.id,
                            name=d.name,
                            unread_count=d.unread_count,
                            type=chat_type,
                            last_message_id=getattr(d.message, "id", None),
                        )
                        results.append(chat)

        except Exception as error:
            logger.warning("get_all_unread_chats_failed", category=type(error).__name__)
            # Partial results must not look like a successful complete scan.
            raise

        return results

    async def _get_unread_chats_by_id(self, chat_ids: List[int]) -> List[Chat]:
        """Read server state in batches without loading avatars or histories."""
        peers = []
        for chat_id in sorted(set(chat_ids)):
            if self._unavailable_chats.get(chat_id, 0) > time.monotonic():
                continue
            try:
                peer = await self.client.get_input_entity(chat_id)
                peers.append(InputDialogPeer(peer=peer))
                self._unavailable_chats.pop(chat_id, None)
            except (ValueError, errors.ChannelPrivateError):
                self._unavailable_chats[chat_id] = time.monotonic() + 300
                logger.warning(
                    "reconcile_chat_unavailable",
                    chat_id=chat_id,
                    retry_after_seconds=300,
                )

        results = []
        for offset in range(0, len(peers), 100):
            response = await self.client(
                GetPeerDialogsRequest(peers=peers[offset : offset + 100])
            )
            entities = {
                utils.get_peer_id(entity): entity
                for entity in [*response.chats, *response.users]
            }
            for dialog in response.dialogs:
                if dialog.unread_count <= 0:
                    continue
                chat_id = utils.get_peer_id(dialog.peer)
                entity = entities.get(chat_id)
                if entity is None:
                    continue
                if isinstance(entity, types.User):
                    chat_type = ChatType.USER
                elif getattr(entity, "forum", False):
                    chat_type = ChatType.FORUM
                elif getattr(entity, "broadcast", False):
                    chat_type = ChatType.CHANNEL
                else:
                    chat_type = ChatType.GROUP
                results.append(
                    Chat(
                        id=chat_id,
                        name=utils.get_display_name(entity),
                        unread_count=dialog.unread_count,
                        type=chat_type,
                        last_message_id=dialog.top_message,
                    )
                )
                logger.info(
                    "autoread_server_unread",
                    chat_id=chat_id,
                    unread_count=dialog.unread_count,
                    read_max_id=dialog.read_inbox_max_id,
                    top_message_id=dialog.top_message,
                )
        return results

    async def get_chat(self, chat_id: int) -> Optional[Chat]:
        try:
            entity = await self.client.get_entity(chat_id)
            name = utils.get_display_name(entity)

            c_type = ChatType.GROUP
            if isinstance(entity, types.User):
                c_type = ChatType.USER
            elif isinstance(entity, types.Chat):
                c_type = ChatType.GROUP
            elif isinstance(entity, types.Channel):
                if getattr(entity, "forum", False):
                    c_type = ChatType.FORUM
                elif getattr(entity, "broadcast", False):
                    c_type = ChatType.CHANNEL
                else:
                    c_type = ChatType.GROUP

            image_url = await self._media._get_chat_image(entity, chat_id)

            unread_topics_count = None
            input_peer = utils.get_input_peer(entity)
            res = await self.client(
                GetPeerDialogsRequest(peers=[InputDialogPeer(peer=input_peer)])  # type: ignore
            )
            if not res.dialogs:
                raise LookupError("Telegram did not return dialog state")
            dialog = res.dialogs[0]
            latest_msg = next(
                (msg for msg in res.messages if msg.id == dialog.top_message), None
            )
            if latest_msg:
                self._parser._cache_message_chat(latest_msg.id, chat_id)
            last_message_preview = format_message_preview(latest_msg, c_type, {})

            return Chat(
                id=chat_id,
                name=name,
                unread_count=dialog.unread_count,
                type=c_type,
                unread_topics_count=unread_topics_count,
                image_url=image_url,
                last_message_preview=last_message_preview,
                is_pinned=dialog.pinned,
            )
        except Exception as error:
            logger.warning(
                "get_chat_failed", chat_id=chat_id, category=type(error).__name__
            )
            raise unavailable(error) from error

    async def get_messages(
        self,
        chat_id: int,
        limit: int = 20,
        topic_id: Optional[int] = None,
        offset_id: int = 0,
        ids: Optional[List[int]] = None,
        after_id: Optional[int] = None,
    ) -> List[Message]:
        if after_id is not None and (offset_id or ids is not None):
            raise ValueError("Newer history cannot be combined with an offset or IDs")
        try:
            entity = await self.client.get_input_entity(chat_id)
            options = {
                "limit": limit,
                "reply_to": topic_id,
                "offset_id": offset_id,
                "ids": ids,
            }
            if after_id is not None:
                options.update(min_id=after_id, reverse=True)
            messages = await self.client.get_messages(entity, **options)
            if after_id is not None:
                messages = list(reversed(messages))
            reply_ids = []
            for msg in messages:
                if not msg:
                    continue
                self._parser._cache_message_chat(msg.id, chat_id)
                reply_header = getattr(msg, "reply_to", None)
                if reply_header:
                    rid = getattr(reply_header, "reply_to_msg_id", None)
                    if rid:
                        reply_ids.append(rid)

            replies_map = {msg.id: msg for msg in messages if msg}
            reply_ids = [rid for rid in set(reply_ids) if rid not in replies_map]
            if reply_ids:
                try:
                    replied_msgs = await self.client.get_messages(entity, ids=reply_ids)
                    for r in replied_msgs:
                        if r:
                            replies_map[r.id] = r
                except Exception:
                    pass

            result_messages = []
            for msg in messages:
                if msg:
                    parsed = await self._parser._parse_message(
                        msg, replies_map, chat_id=chat_id
                    )
                    result_messages.append(parsed)
            try:
                self._media.prefetch_previews(
                    chat_id, [msg for msg in messages if msg], result_messages
                )
            except Exception as error:
                logger.warning(
                    "preview_prefetch_failed", chat_id=chat_id, error=repr(error)
                )
            return result_messages
        except Exception as error:
            logger.warning(
                "get_messages_failed", chat_id=chat_id, category=type(error).__name__
            )
            raise unavailable(error) from error

    async def get_message_context(
        self, chat_id: int, message_id: int
    ) -> Optional[MessageContext]:
        """Fetch one message and its actual forum scope for a dry run."""
        try:
            entity = await self.client.get_entity(chat_id)
            if entity is None:
                raise LookupError("Telegram did not return the chat")
            messages = await self.client.get_messages(entity, ids=[message_id])
            message = next((item for item in messages if item), None)
            if message is None:
                return None
            is_forum = bool(getattr(entity, "forum", False))
            topic_id = self._parser._extract_topic_id(message) if is_forum else None
            parsed = await self._parser._parse_message(message, chat_id=chat_id)
            return MessageContext(
                parsed, topic_id, not is_forum or topic_id is not None
            )
        except Exception as error:
            logger.warning(
                "get_message_context_failed",
                chat_id=chat_id,
                category=type(error).__name__,
            )
            raise unavailable(error) from error

    async def get_recent_authors(
        self, chat_id: int, limit: int = 100
    ) -> List[Dict[str, Any]]:
        try:
            entity = await self.client.get_entity(chat_id)
            messages = await self.client.get_messages(entity, limit=limit)

            seen_ids = set()
            authors = []

            for m in messages:
                sender = getattr(m, "sender", None)
                if sender:
                    sid = sender.id
                    if sid in seen_ids:
                        continue
                    seen_ids.add(sid)

                    name = utils.get_display_name(sender)
                    username = getattr(sender, "username", None)

                    avatar_url = await self._media._get_chat_image(sender, sid)

                    authors.append(
                        {
                            "id": sid,
                            "name": name,
                            "username": f"@{username}" if username else None,
                            "avatar_url": avatar_url,
                        }
                    )

            return authors
        except Exception as error:
            logger.warning(
                "get_recent_authors_failed",
                chat_id=chat_id,
                category=type(error).__name__,
            )
            raise unavailable(error) from error

    async def get_self_premium_status(self) -> bool:
        try:
            if not self.client:
                raise LookupError("Telegram client unavailable")
            from telethon import functions

            users = await self.client(
                functions.users.GetUsersRequest(id=[types.InputUserSelf()])
            )
            if not users or not isinstance(users[0], types.User):
                raise LookupError("Telegram did not return the account")
            return bool(users[0].premium)
        except Exception as error:
            logger.warning("get_premium_status_failed", category=type(error).__name__)
            raise unavailable(error) from error
