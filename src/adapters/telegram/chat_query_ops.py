import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from telethon import types, utils
from telethon.tl.functions.messages import GetPeerDialogsRequest
from telethon.tl.types import InputDialogPeer

from src.adapters.telethon_mappers import (
    format_message_preview,
    map_telethon_dialog_to_chat_type,
)
from src.domain.models import Chat, ChatType, Message
from src.infrastructure.logging import get_logger

if TYPE_CHECKING:
    from src.adapters.telegram.media import MediaManager
    from src.adapters.telegram.message_parser import MessageParser

logger = get_logger(__name__)


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

    async def get_chats(self, limit: int) -> list[Chat]:
        dialogs = await self.client.get_dialogs(limit=limit)
        results = []
        for d in dialogs:
            chat_type = map_telethon_dialog_to_chat_type(d)
            unread_count = d.unread_count
            unread_topics_count = None

            image_url = await self._media._get_chat_image(d.entity, d.id)

            msg = getattr(d, "message", None)
            if msg:
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

    async def get_all_unread_chats(self) -> List[Chat]:
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
                        )
                        results.append(chat)

        except Exception as e:
            logger.error("get_all_unread_chats_failed", error=str(e))

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

            unread_count = 0
            unread_topics_count = None
            last_message_preview = None
            is_pinned = False

            try:
                input_peer = utils.get_input_peer(entity)
                res = await self.client(
                    GetPeerDialogsRequest(peers=[InputDialogPeer(peer=input_peer)])  # type: ignore
                )
                if res.dialogs:
                    dialog = res.dialogs[0]
                    unread_count = dialog.unread_count
                    is_pinned = dialog.pinned
            except Exception as e:
                logger.warning(
                    "fetch_dialog_stats_failed", chat_id=chat_id, error=str(e)
                )

            try:
                messages = await self.client.get_messages(entity, limit=1)
                if messages:
                    latest_msg = messages[0]
                    self._parser._cache_message_chat(latest_msg.id, chat_id)
                    last_message_preview = format_message_preview(
                        latest_msg, c_type, {}
                    )
                else:
                    last_message_preview = "No messages"
            except Exception as e:
                logger.warning(
                    "fetch_latest_message_failed", chat_id=chat_id, error=str(e)
                )

            return Chat(
                id=chat_id,
                name=name,
                unread_count=unread_count,
                type=c_type,
                unread_topics_count=unread_topics_count,
                image_url=image_url,
                last_message_preview=last_message_preview,
                is_pinned=is_pinned,
            )
        except Exception as e:
            logger.error("get_chat_failed", chat_id=chat_id, error=str(e))
            return None

    async def get_messages(
        self,
        chat_id: int,
        limit: int = 20,
        topic_id: Optional[int] = None,
        offset_id: int = 0,
        ids: Optional[List[int]] = None,
    ) -> List[Message]:
        try:
            entity = await self.client.get_entity(chat_id)
            messages = await self.client.get_messages(
                entity, limit=limit, reply_to=topic_id, offset_id=offset_id, ids=ids
            )
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

            replies_map = {}
            if reply_ids:
                try:
                    reply_ids = list(set(reply_ids))
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
            return result_messages
        except Exception as e:
            logger.error("get_messages_failed", chat_id=chat_id, error=str(e))
            return []

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
        except Exception as e:
            logger.error("get_recent_authors_failed", chat_id=chat_id, error=str(e))
            return []

    async def get_self_premium_status(self) -> bool:
        try:
            if not self.client:
                return False
            from telethon import functions

            users = await self.client(
                functions.users.GetUsersRequest(id=[types.InputUserSelf()])
            )
            if users:
                return getattr(users[0], "premium", False)
            return False
        except Exception as e:
            logger.error("get_premium_status_failed", error=str(e))
            return False
