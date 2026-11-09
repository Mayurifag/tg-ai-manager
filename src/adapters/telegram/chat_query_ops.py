import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from telethon import errors, types, utils
from telethon.tl.functions.messages import GetPeerDialogsRequest
from telethon.tl.types import InputDialogPeer

from src.adapters.telethon_mappers import (
    format_message_preview,
    map_telethon_dialog_to_chat_type,
)
from src.adapters.telegram.query_errors import unavailable
from src.domain.errors import AccountChanged
from src.domain.models import Chat, ChatType, Message, MessageContext
from src.infrastructure.logging import get_logger
from src.infrastructure.tasks import raise_if_task_stopped

if TYPE_CHECKING:
    from src.adapters.telegram.media import MediaManager
    from src.adapters.telegram.message_parser import MessageParser

logger = get_logger(__name__)

# Loading the complete list must not evict the live message-to-chat cache.
RECENT_DIALOG_CACHE_LIMIT = 20
# shortcut: retain up to 10,000 inaccessible-chat backoffs; revisit if a
# single account has more configured inaccessible scopes than this.
MAX_UNAVAILABLE_CHATS = 10_000


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
        self._unavailable_chats: OrderedDict[int, float] = OrderedDict()

    async def get_chats(self) -> list[Chat]:
        raise_if_task_stopped()
        avatar_generation = self._media.avatar_generation()
        raise_if_task_stopped()
        try:
            dialogs = await self.client.get_dialogs(limit=None)
            raise_if_task_stopped()
        except Exception as error:
            raise_if_task_stopped()
            logger.warning("get_chats_failed", category=type(error).__name__)
            raise unavailable(error) from error
        results = []
        for d in dialogs:
            raise_if_task_stopped()
            chat_type = map_telethon_dialog_to_chat_type(d)
            unread_count = d.unread_count
            unread_topics_count = None

            raise_if_task_stopped()
            image_url = await self._media._get_chat_image(
                d.entity, d.id, generation=avatar_generation
            )
            raise_if_task_stopped()

            msg = getattr(d, "message", None)
            if msg and len(results) < RECENT_DIALOG_CACHE_LIMIT:
                raise_if_task_stopped()
                self._parser._cache_message_chat(msg.id, d.id)

            raise_if_task_stopped()
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
        raise_if_task_stopped()
        return results

    async def get_all_unread_chats(
        self,
        chat_ids: Optional[List[int]] = None,
        *,
        is_current: Optional[Callable[[], bool]] = None,
    ) -> List[Chat]:
        def check() -> None:
            raise_if_task_stopped()
            if is_current is not None and not is_current():
                raise AccountChanged()

        check()
        try:
            if chat_ids is not None:
                return await self._get_unread_chats_by_id(chat_ids, check=check)
            results = []
            folders = [0, 1]
            for folder_id in folders:
                check()
                count = 0
                async for d in self.client.iter_dialogs(
                    limit=None, ignore_migrated=True, folder=folder_id
                ):
                    check()
                    count += 1
                    if count % 50 == 0:
                        await asyncio.sleep(0)
                        check()

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
                    check()
                check()

        except Exception as error:
            check()
            logger.warning("get_all_unread_chats_failed", category=type(error).__name__)
            # Partial results must not look like a successful complete scan.
            raise

        check()
        return results

    async def _get_unread_chats_by_id(
        self, chat_ids: List[int], *, check: Callable[[], None]
    ) -> List[Chat]:
        """Read server state in batches without loading avatars or histories."""
        check()
        now = time.monotonic()
        for chat_id, retry_at in list(self._unavailable_chats.items()):
            check()
            if retry_at <= now:
                self._unavailable_chats.pop(chat_id, None)
        peers = []
        for chat_id in sorted(set(chat_ids)):
            check()
            if self._unavailable_chats.get(chat_id, 0) > time.monotonic():
                continue
            try:
                peer = await self.client.get_input_entity(chat_id)
                check()
                if not peer:
                    raise LookupError("Telegram did not return the chat")
                check()
                peers.append(InputDialogPeer(peer=peer))
                self._unavailable_chats.pop(chat_id, None)
            except (ValueError, errors.ChannelPrivateError):
                check()
                self._unavailable_chats[chat_id] = time.monotonic() + 300
                self._unavailable_chats.move_to_end(chat_id)
                while len(self._unavailable_chats) > MAX_UNAVAILABLE_CHATS:
                    self._unavailable_chats.popitem(last=False)
                logger.warning(
                    "reconcile_chat_unavailable",
                    chat_id=chat_id,
                    retry_after_seconds=300,
                )

        results = []
        for offset in range(0, len(peers), 100):
            check()
            response = await self.client(
                GetPeerDialogsRequest(peers=peers[offset : offset + 100])
            )
            check()
            entities = {
                utils.get_peer_id(entity): entity
                for entity in [*response.chats, *response.users]
            }
            check()
            for dialog in response.dialogs:
                check()
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
        check()
        return results

    async def get_chat(
        self, chat_id: int, *, is_current: Optional[Callable[[], bool]] = None
    ) -> Optional[Chat]:
        def check() -> None:
            raise_if_task_stopped()
            if is_current is not None and not is_current():
                raise AccountChanged()

        check()
        avatar_generation = self._media.avatar_generation()
        check()
        try:
            entity = await self.client.get_entity(chat_id)
            check()
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

            check()
            image_url = await self._media._get_chat_image(
                entity, chat_id, generation=avatar_generation
            )
            check()

            unread_topics_count = None
            input_peer = utils.get_input_peer(entity)
            check()
            res = await self.client(
                GetPeerDialogsRequest(peers=[InputDialogPeer(peer=input_peer)])  # type: ignore
            )
            check()
            if not res.dialogs:
                raise LookupError("Telegram did not return dialog state")
            dialog = res.dialogs[0]
            latest_msg = next(
                (msg for msg in res.messages if msg.id == dialog.top_message), None
            )
            check()
            if latest_msg:
                self._parser._cache_message_chat(latest_msg.id, chat_id)
            check()
            last_message_preview = format_message_preview(latest_msg, c_type, {})

            check()
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
        except AccountChanged:
            raise
        except Exception as error:
            check()
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
        *,
        is_current: Optional[Callable[[], bool]] = None,
    ) -> List[Message]:
        def check() -> None:
            raise_if_task_stopped()
            if is_current is not None and not is_current():
                raise AccountChanged()

        check()
        try:
            generation = self._media.message_generation(chat_id)
            check()
            avatar_generation = self._media.avatar_generation()
            check()
            entity = await self.client.get_input_entity(chat_id)
            check()
            # An unresolved peer must not become an unscoped Telegram lookup.
            if not entity:
                raise LookupError("Telegram did not return the chat")
            check()
            messages = await self.client.get_messages(
                entity, limit=limit, reply_to=topic_id, offset_id=offset_id, ids=ids
            )
            check()
            reply_ids = []
            for msg in messages:
                check()
                if not msg:
                    continue
                self._parser._cache_message_chat(msg.id, chat_id)
                check()
                reply_header = getattr(msg, "reply_to", None)
                if reply_header:
                    rid = getattr(reply_header, "reply_to_msg_id", None)
                    if rid:
                        reply_ids.append(rid)

            replies_map = {msg.id: msg for msg in messages if msg}
            reply_ids = [rid for rid in set(reply_ids) if rid not in replies_map]
            if reply_ids:
                try:
                    check()
                    replied_msgs = await self.client.get_messages(entity, ids=reply_ids)
                    check()
                    for r in replied_msgs:
                        check()
                        if r:
                            replies_map[r.id] = r
                except AccountChanged:
                    raise
                except Exception:
                    check()
                    pass

            result_messages = []
            for msg in messages:
                check()
                if msg:
                    parsed = await self._parser._parse_message(
                        msg,
                        replies_map,
                        chat_id=chat_id,
                        avatar_generation=avatar_generation,
                        is_current=is_current,
                    )
                    check()
                    result_messages.append(parsed)
            try:
                check()
                self._media.prefetch_previews(
                    chat_id,
                    [msg for msg in messages if msg],
                    result_messages,
                    generation=generation,
                )
                check()
            except AccountChanged:
                raise
            except Exception as error:
                check()
                logger.warning(
                    "preview_prefetch_failed",
                    chat_id=chat_id,
                    category=type(error).__name__,
                )
            check()
            return result_messages
        except AccountChanged:
            raise
        except Exception as error:
            check()
            logger.warning(
                "get_messages_failed", chat_id=chat_id, category=type(error).__name__
            )
            raise unavailable(error) from error

    async def get_message_context(
        self,
        chat_id: int,
        message_id: int,
        *,
        is_current: Optional[Callable[[], bool]] = None,
    ) -> Optional[MessageContext]:
        """Fetch one message and its actual forum scope for a dry run."""

        def check() -> None:
            raise_if_task_stopped()
            if is_current is not None and not is_current():
                raise AccountChanged()

        check()
        try:
            avatar_generation = self._media.avatar_generation()
            check()
            entity = await self.client.get_entity(chat_id)
            check()
            # A falsey entity permits an unscoped message-ID lookup in Telethon.
            if not entity:
                raise LookupError("Telegram did not return the chat")
            check()
            messages = await self.client.get_messages(entity, ids=[message_id])
            check()
            message = next((item for item in messages if item), None)
            check()
            if message is None:
                return None
            is_forum = bool(getattr(entity, "forum", False))
            check()
            topic_id = self._parser._extract_topic_id(message) if is_forum else None
            check()
            parsed = await self._parser._parse_message(
                message,
                chat_id=chat_id,
                avatar_generation=avatar_generation,
                is_current=is_current,
            )
            check()
            return MessageContext(
                parsed, topic_id, not is_forum or topic_id is not None
            )
        except AccountChanged:
            raise
        except Exception as error:
            check()
            logger.warning(
                "get_message_context_failed",
                chat_id=chat_id,
                category=type(error).__name__,
            )
            raise unavailable(error) from error

    async def get_recent_authors(
        self,
        chat_id: int,
        limit: int = 100,
        *,
        is_current: Optional[Callable[[], bool]] = None,
    ) -> List[Dict[str, Any]]:
        def check() -> None:
            raise_if_task_stopped()
            if is_current is not None and not is_current():
                raise AccountChanged()

        check()
        try:
            avatar_generation = self._media.avatar_generation()
            check()
            entity = await self.client.get_entity(chat_id)
            check()
            # Telethon treats a falsey entity as a global message search.
            if not entity:
                raise LookupError("Telegram did not return the chat")
            check()
            messages = await self.client.get_messages(entity, limit=limit)
            check()

            seen_ids = set()
            authors = []

            for m in messages:
                check()
                sender = getattr(m, "sender", None)
                if sender:
                    sid = utils.get_peer_id(sender)
                    if sid in seen_ids:
                        continue
                    seen_ids.add(sid)

                    name = utils.get_display_name(sender)
                    username = getattr(sender, "username", None)

                    check()
                    avatar_url = await self._media._get_chat_image(
                        sender, sid, generation=avatar_generation
                    )
                    check()

                    authors.append(
                        {
                            "id": sid,
                            "name": name,
                            "username": f"@{username}" if username else None,
                            "avatar_url": avatar_url,
                        }
                    )

            check()
            return authors
        except AccountChanged:
            raise
        except Exception as error:
            check()
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
