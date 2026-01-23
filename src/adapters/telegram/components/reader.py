import asyncio
from typing import Any, Dict, List, Optional

from telethon import functions, types, utils
from telethon.tl.types import InputDialogPeer, MessageActionTopicCreate

from src.adapters.telethon_mappers import (
    format_message_preview,
    map_telethon_dialog_to_chat_type,
)
from src.domain.models import Chat, ChatType, Message
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class TelegramChatReader:
    def __init__(self, client: Any, media_component: Any, parser_component: Any):
        self.client = client
        self.media = media_component
        self.parser = parser_component
        # Internal cache for mapping msg_id -> chat_id for events
        self._msg_id_map: Dict[int, int] = {}

    def cache_message_chat(self, msg_id: int, chat_id: int):
        if len(self._msg_id_map) > 15000:
            self._msg_id_map.clear()
        self._msg_id_map[msg_id] = chat_id

    def get_chat_id_by_msg(self, msg_id: int) -> Optional[int]:
        return self._msg_id_map.get(msg_id)

    async def get_chats(self, limit: int) -> list[Chat]:
        dialogs = await self.client.get_dialogs(limit=limit)
        results = []
        for d in dialogs:
            chat_type = map_telethon_dialog_to_chat_type(d)
            image_url = await self.media.get_chat_image_url(d.entity, d.id)

            msg = getattr(d, "message", None)
            if msg:
                self.cache_message_chat(msg.id, d.id)

            preview = format_message_preview(msg, chat_type, {})

            results.append(
                Chat(
                    id=d.id,
                    name=d.name,
                    unread_count=d.unread_count,
                    type=chat_type,
                    last_message_preview=preview,
                    image_url=image_url,
                    is_pinned=d.pinned,
                )
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

            image_url = await self.media.get_chat_image_url(entity, chat_id)

            unread_count = 0
            is_pinned = False
            last_message_preview = None

            # Get Stats
            try:
                input_peer = utils.get_input_peer(entity)
                res = await self.client(
                    functions.messages.GetPeerDialogsRequest(
                        peers=[InputDialogPeer(peer=input_peer)]
                    )
                )
                if res.dialogs:
                    d = res.dialogs[0]
                    unread_count = d.unread_count
                    is_pinned = d.pinned
            except Exception:
                pass

            # Latest Msg
            try:
                msgs = await self.client.get_messages(entity, limit=1)
                if msgs:
                    self.cache_message_chat(msgs[0].id, chat_id)
                    last_message_preview = format_message_preview(msgs[0], c_type, {})
                else:
                    last_message_preview = "No messages"
            except Exception:
                pass

            return Chat(
                id=chat_id,
                name=name,
                unread_count=unread_count,
                type=c_type,
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
                if msg:
                    self.cache_message_chat(msg.id, chat_id)
                    rh = getattr(msg, "reply_to", None)
                    if rh and getattr(rh, "reply_to_msg_id", None):
                        reply_ids.append(rh.reply_to_msg_id)

            replies_map = {}
            if reply_ids:
                try:
                    r_msgs = await self.client.get_messages(
                        entity, ids=list(set(reply_ids))
                    )
                    for r in r_msgs:
                        if r:
                            replies_map[r.id] = r
                except Exception:
                    pass

            results = []
            for msg in messages:
                if msg:
                    parsed = await self.parser.parse(msg, replies_map, chat_id=chat_id)
                    results.append(parsed)
            return results
        except Exception as e:
            logger.error("get_messages_failed", chat_id=chat_id, error=str(e))
            return []

    async def get_recent_authors(
        self, chat_id: int, limit: int = 100
    ) -> List[Dict[str, Any]]:
        try:
            entity = await self.client.get_entity(chat_id)
            messages = await self.client.get_messages(entity, limit=limit)
            seen = set()
            authors = []

            for m in messages:
                sender = getattr(m, "sender", None)
                if sender:
                    sid = sender.id
                    if sid in seen:
                        continue
                    seen.add(sid)

                    avatar = await self.media.get_chat_image_url(sender, sid)
                    authors.append(
                        {
                            "id": sid,
                            "name": utils.get_display_name(sender),
                            "username": f"@{sender.username}"
                            if getattr(sender, "username", None)
                            else None,
                            "avatar_url": avatar,
                        }
                    )
            return authors
        except Exception as e:
            logger.error("get_recent_authors_failed", chat_id=chat_id, error=str(e))
            return []

    async def get_forum_topics(self, chat_id: int, limit: int = 20) -> List[Chat]:
        try:
            entity = await self.client.get_entity(chat_id)
            res = await self.client(
                functions.messages.GetForumTopicsRequest(
                    peer=entity,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=limit,
                    q="",
                )
            )
            if not res:
                return []

            valid = [
                t for t in res.topics if not isinstance(t, types.ForumTopicDeleted)
            ]

            # Fetch top messages for previews
            top_ids = [t.top_message for t in valid]
            msg_map = {}
            if top_ids:
                msgs = await self.client.get_messages(entity, ids=top_ids)
                for m in msgs:
                    if m:
                        msg_map[m.id] = m

            topics = []
            for t in valid:
                last = msg_map.get(t.top_message)
                preview = format_message_preview(last, ChatType.TOPIC)
                topics.append(
                    Chat(
                        id=t.id,
                        name=t.title,
                        unread_count=t.unread_count,
                        type=ChatType.TOPIC,
                        last_message_preview=preview,
                        icon_emoji=getattr(t, "icon_emoji", None),
                    )
                )
            return topics
        except Exception as e:
            logger.error("get_forum_topics_failed", chat_id=chat_id, error=str(e))
            return []

    async def get_unread_topics(self, chat_id: int) -> List[Chat]:
        try:
            entity = await self.client.get_entity(chat_id)
            res = await self.client(
                functions.messages.GetForumTopicsRequest(
                    peer=entity,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=100,
                    q="",
                )
            )
            topics = []
            if not res:
                return []
            for t in res.topics:
                if not isinstance(t, types.ForumTopicDeleted) and t.unread_count > 0:
                    topics.append(
                        Chat(
                            id=t.id,
                            name=t.title,
                            unread_count=t.unread_count,
                            type=ChatType.TOPIC,
                        )
                    )
            return topics
        except Exception:
            return []

    async def get_topic_name(self, chat_id: int, topic_id: int) -> Optional[str]:
        try:
            entity = await self.client.get_entity(chat_id)
            msgs = await self.client.get_messages(entity, ids=[topic_id])
            if (
                msgs
                and msgs[0]
                and isinstance(
                    getattr(msgs[0], "action", None), MessageActionTopicCreate
                )
            ):
                return msgs[0].action.title
        except Exception:
            pass
        return None

    async def get_all_unread_chats(self) -> List[Chat]:
        results = []
        try:
            count = 0
            async for d in self.client.iter_dialogs(limit=None, ignore_migrated=True):
                count += 1
                if count % 50 == 0:
                    await asyncio.sleep(0)
                if d.unread_count > 0 or d.unread_mentions_count > 0:
                    chat_type = map_telethon_dialog_to_chat_type(d)
                    results.append(
                        Chat(
                            id=d.id,
                            name=d.name,
                            unread_count=d.unread_count,
                            type=chat_type,
                        )
                    )
        except Exception as e:
            logger.error("get_all_unread_chats_failed", error=str(e))
        return results
