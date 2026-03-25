from typing import Any, Dict, List, Optional

from telethon import functions, types
from telethon.tl.types import MessageActionTopicCreate

from src.adapters.telethon_mappers import format_message_preview
from src.domain.models import Chat, ChatType
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class ForumOps:
    def __init__(self, client: Any) -> None:
        self.client = client
        self._topic_name_cache: Dict[tuple[int, int], str] = {}

    async def _fetch_forum_topics_response(
        self, peer: Any, limit: int
    ) -> Optional[Any]:
        try:
            return await self.client(
                functions.messages.GetForumTopicsRequest(
                    peer=peer,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=limit,
                    q="",
                )
            )
        except Exception as e:
            logger.error("fetch_topics_failed", error=str(e))
            return None

    async def _get_top_messages_map(
        self, entity: Any, top_message_ids: List[int]
    ) -> Dict[int, Any]:
        messages_map = {}
        if top_message_ids:
            try:
                msgs = await self.client.get_messages(entity, ids=top_message_ids)
                if msgs:
                    for m in msgs:
                        if m:
                            messages_map[m.id] = m
            except Exception:
                pass
        return messages_map

    async def get_forum_topics(self, chat_id: int, limit: int = 20) -> List[Chat]:
        try:
            entity = await self.client.get_entity(chat_id)
            response = await self._fetch_forum_topics_response(entity, limit=limit)
            topics = []
            if not response:
                return topics

            valid_topics = [
                t for t in response.topics if not isinstance(t, types.ForumTopicDeleted)
            ]

            top_message_ids = [t.top_message for t in valid_topics]
            messages_map = await self._get_top_messages_map(entity, top_message_ids)
            for t in valid_topics:
                last_msg = messages_map.get(t.top_message)
                preview = format_message_preview(last_msg, ChatType.TOPIC)
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
            response = await self.client(
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
            if not response:
                return topics

            for t in response.topics:
                if isinstance(t, types.ForumTopicDeleted):
                    continue
                if t.unread_count > 0:
                    topics.append(
                        Chat(
                            id=t.id,
                            name=t.title,
                            unread_count=t.unread_count,
                            type=ChatType.TOPIC,
                        )
                    )
            return topics
        except Exception as e:
            logger.error("get_unread_topics_failed", chat_id=chat_id, error=str(e))
            return []

    async def get_topic_name(self, chat_id: int, topic_id: int) -> Optional[str]:
        key = (chat_id, topic_id)
        if key in self._topic_name_cache:
            return self._topic_name_cache[key]

        try:
            entity = await self.client.get_entity(chat_id)
            messages = await self.client.get_messages(entity, ids=[topic_id])

            if messages:
                message = messages[0]
                if message and getattr(message, "action", None):
                    if isinstance(message.action, MessageActionTopicCreate):
                        name = message.action.title
                        self._topic_name_cache[key] = name
                        return name
        except Exception as e:
            logger.error(
                "get_topic_name_failed",
                chat_id=chat_id,
                topic_id=topic_id,
                error=str(e),
            )
        return None
