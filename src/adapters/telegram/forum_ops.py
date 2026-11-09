import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, AsyncIterator, List, Optional

from telethon import functions, types
from src.adapters.telegram.query_errors import unavailable
from src.adapters.telethon_mappers import format_message_preview
from src.domain.errors import AccountChanged
from src.domain.models import Chat, ChatType, ForumTopicCursor, ForumTopicPage
from src.infrastructure.logging import get_logger
from src.infrastructure.tasks import raise_if_task_stopped

logger = get_logger(__name__)
TOPIC_PAGE_SIZE = 100
MAX_TOPIC_PAGES = 100
TOPIC_NAME_TTL = 60
MAX_TOPIC_NAMES = 1024
TOPIC_NAME_LOOKUP_ATTEMPTS = 3


class ForumOps:
    def __init__(self, client: Any) -> None:
        self.client = client
        self._topic_name_cache: OrderedDict[tuple[int, int], tuple[float, str]] = (
            OrderedDict()
        )
        self._topic_cache_generation = 0

    async def _fetch_forum_topics_response(
        self, peer: Any, limit: int, cursor: Optional[ForumTopicCursor] = None
    ) -> Any:
        return await self.client(
            functions.messages.GetForumTopicsRequest(
                peer=peer,
                offset_date=cursor.date if cursor else None,
                offset_id=cursor.message_id if cursor else 0,
                offset_topic=cursor.topic_id if cursor else 0,
                limit=limit,
                q="",
            )
        )

    async def get_forum_topics(
        self,
        chat_id: int,
        limit: int = 20,
        *,
        is_current: Optional[Callable[[], bool]] = None,
    ) -> List[Chat]:
        return (
            await self.get_forum_topics_page(
                chat_id, limit=limit, is_current=is_current
            )
        ).topics

    async def get_forum_topics_page(
        self,
        chat_id: int,
        cursor: Optional[ForumTopicCursor] = None,
        *,
        limit: int = 20,
        is_current: Optional[Callable[[], bool]] = None,
    ) -> ForumTopicPage:
        def check() -> None:
            raise_if_task_stopped()
            if is_current is not None and not is_current():
                raise AccountChanged()

        check()
        try:
            entity = await self.client.get_entity(chat_id)
            check()
            if not entity:
                raise LookupError("Telegram did not return the forum")
            check()
            response = await self._fetch_forum_topics_response(entity, limit, cursor)
            check()
            if not response:
                raise LookupError("Telegram did not return forum topics")
            next_cursor = self._topic_cursor(response)
            check()
            if cursor and next_cursor == cursor:
                raise RuntimeError("Forum topic pagination did not advance")
            topics = self._topic_chats(response)
            check()
            return ForumTopicPage(topics, next_cursor)
        except AccountChanged:
            raise
        except Exception as error:
            check()
            logger.warning(
                "get_forum_topics_failed",
                chat_id=chat_id,
                category=type(error).__name__,
            )
            raise unavailable(error) from error

    async def get_forum_topic(
        self,
        chat_id: int,
        topic_id: int,
        *,
        is_current: Optional[Callable[[], bool]] = None,
    ) -> Optional[Chat]:
        def check() -> None:
            raise_if_task_stopped()
            if is_current is not None and not is_current():
                raise AccountChanged()

        check()
        try:
            entity = await self.client.get_entity(chat_id)
            check()
            if not entity:
                raise LookupError("Telegram did not return the forum")
            check()
            response = await self.client(
                functions.messages.GetForumTopicsByIDRequest(
                    peer=entity, topics=[topic_id]
                )
            )
            check()
            topic = next(
                (
                    topic
                    for topic in self._topic_chats(response)
                    if topic.id == topic_id
                ),
                None,
            )
            check()
            return topic
        except AccountChanged:
            raise
        except Exception as error:
            check()
            logger.warning(
                "get_forum_topic_failed", chat_id=chat_id, category=type(error).__name__
            )
            raise unavailable(error) from error

    @staticmethod
    def _topic_chats(response: Any) -> List[Chat]:
        messages = {message.id: message for message in response.messages}
        return [
            Chat(
                id=topic.id,
                name=topic.title,
                unread_count=topic.unread_count,
                type=ChatType.TOPIC,
                last_message_preview=format_message_preview(
                    messages.get(topic.top_message), ChatType.TOPIC
                ),
                icon_emoji=getattr(topic, "icon_emoji", None),
            )
            for topic in response.topics
            if not isinstance(topic, types.ForumTopicDeleted)
        ]

    @staticmethod
    def _topic_cursor(response: Any) -> Optional[ForumTopicCursor]:
        if not response.topics:
            return None
        valid = [
            topic
            for topic in response.topics
            if not isinstance(topic, types.ForumTopicDeleted)
        ]
        if not valid:
            raise RuntimeError("Cannot advance through deleted forum topics")
        last = valid[-1]
        last_message = next(
            (
                message
                for message in response.messages
                if message.id == last.top_message
            ),
            None,
        )
        date = (
            last.date
            if getattr(response, "order_by_create_date", False) or last_message is None
            else last_message.date
        )
        if date is None:
            raise RuntimeError("Forum topic page has no pagination date")
        return ForumTopicCursor(date, last.top_message, last.id)

    async def _iter_topic_pages(
        self, entity: Any, *, check: Callable[[], None]
    ) -> AsyncIterator[Any]:
        cursor = None
        seen_cursors = set()
        for _ in range(MAX_TOPIC_PAGES):
            check()
            response = await self._fetch_forum_topics_response(
                entity, TOPIC_PAGE_SIZE, cursor
            )
            check()
            if not response.topics:
                return
            check()
            yield response
            check()
            cursor = self._topic_cursor(response)
            check()
            if cursor in seen_cursors:
                raise RuntimeError("Forum topic pagination did not advance")
            seen_cursors.add(cursor)
        check()
        raise RuntimeError("Forum topic pagination limit reached")

    async def get_unread_topics(
        self,
        chat_id: int,
        topic_ids: Optional[List[int]] = None,
        *,
        is_current: Optional[Callable[[], bool]] = None,
    ) -> List[Chat]:
        """Fetch all unread topics, or just the explicitly approved IDs."""

        def check() -> None:
            raise_if_task_stopped()
            if is_current is not None and not is_current():
                raise AccountChanged()

        check()
        try:
            entity = await self.client.get_entity(chat_id)
            check()
            if not entity:
                raise LookupError("Telegram did not return the forum")
            check()
            topics: dict[int, Chat] = {}

            def add(response: Any) -> None:
                check()
                for topic in response.topics:
                    check()
                    if (
                        not isinstance(topic, types.ForumTopicDeleted)
                        and topic.unread_count > 0
                    ):
                        topics[topic.id] = Chat(
                            id=topic.id,
                            name=topic.title,
                            unread_count=topic.unread_count,
                            type=ChatType.TOPIC,
                            last_message_id=topic.top_message,
                        )
                check()

            if topic_ids is not None:
                unique_ids = sorted(set(topic_ids))
                check()
                for offset in range(0, len(unique_ids), TOPIC_PAGE_SIZE):
                    check()
                    response = await self.client(
                        functions.messages.GetForumTopicsByIDRequest(
                            peer=entity,
                            topics=unique_ids[offset : offset + TOPIC_PAGE_SIZE],
                        )
                    )
                    check()
                    add(response)
            else:
                async for response in self._iter_topic_pages(entity, check=check):
                    add(response)
            check()
            return list(topics.values())
        except Exception:
            check()
            raise

    async def get_topic_name(
        self, chat_id: int, topic_id: int, deadline: Optional[float] = None
    ) -> Optional[str]:
        def within_deadline() -> bool:
            raise_if_task_stopped()
            return deadline is None or asyncio.get_running_loop().time() < deadline

        key = (chat_id, topic_id)
        try:
            for _ in range(TOPIC_NAME_LOOKUP_ATTEMPTS):
                if not within_deadline():
                    return None
                cached = self._topic_name_cache.get(key)
                if cached and time.monotonic() - cached[0] < TOPIC_NAME_TTL:
                    self._topic_name_cache.move_to_end(key)
                    return cached[1]
                self._topic_name_cache.pop(key, None)
                generation = self._topic_cache_generation
                entity = await self.client.get_entity(chat_id)
                if not within_deadline():
                    return None
                response = await self.client(
                    functions.messages.GetForumTopicsByIDRequest(
                        peer=entity, topics=[topic_id]
                    )
                )
                if not within_deadline():
                    return None
                if generation != self._topic_cache_generation:
                    continue
                topic = next(
                    (
                        item
                        for item in response.topics
                        if item.id == topic_id
                        and not isinstance(item, types.ForumTopicDeleted)
                    ),
                    None,
                )
                if not topic:
                    return None
                name = topic.title
                if not within_deadline():
                    return None
                self._topic_name_cache[key] = (time.monotonic(), name)
                self._topic_name_cache.move_to_end(key)
                while len(self._topic_name_cache) > MAX_TOPIC_NAMES:
                    self._topic_name_cache.popitem(last=False)
                return name
        except Exception as e:
            raise_if_task_stopped()
            logger.error(
                "get_topic_name_failed",
                chat_id=chat_id,
                topic_id=topic_id,
                category=type(e).__name__,
            )
        finally:
            raise_if_task_stopped()
        return None

    def invalidate_topic_name(self, chat_id: int, topic_id: int) -> None:
        self._topic_cache_generation += 1
        self._topic_name_cache.pop((chat_id, topic_id), None)
