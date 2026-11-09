import asyncio
import math
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional
from weakref import WeakValueDictionary

from src.application.message_views import group_messages_into_albums
from src.domain.errors import AccountChanged, TelegramUnavailable
from src.domain.models import (
    ActionLog,
    Chat,
    ChatType,
    ForumTopicCursor,
    ForumTopicPage,
    MediaContent,
    Message,
    SystemEvent,
)
from src.domain.ports import ActionRepository, ChatRepository, EventRepository
from src.infrastructure.action_logging import record_action
from src.infrastructure.logging import get_logger
from src.infrastructure.tasks import raise_if_task_stopped


CHAT_PAGE_SIZE = 20
CHAT_SNAPSHOT_TTL = 60 * 60
MAX_CHAT_SNAPSHOTS = 8
CHAT_LIST_TTL = 60
MESSAGE_CACHE_TTL = 300
EMPTY_CACHE_TTL = 5
MAX_MESSAGE_CACHES = 100
# shortcut: bound auxiliary chat metadata; revisit if observed accounts need
# more concurrently active histories or forums than these cache sizes.
MAX_MESSAGE_VERSIONS = 2048
MAX_FORUM_TOPIC_CACHES = 100
MAX_EVENT_PREFETCH_TRACKED = 1000
PREFETCH_WORKERS = 3
EVENT_PREFETCH_INTERVAL = 20
HISTORY_PAGE_SIZE = 20
# Look ahead near a history boundary without scanning an unbounded chat.
ALBUM_BOUNDARY_BAND = 10
ALBUM_BOUNDARY_LOOKAHEAD = 20
MAX_ALBUM_BOUNDARY_EXTENSION = 40
logger = get_logger(__name__)


@dataclass
class ChatPage:
    chats: List[Chat]
    snapshot: str
    next_offset: Optional[int]


class ChatSnapshotExpired(Exception):
    pass


class ChatInteractor:
    def __init__(
        self,
        repository: ChatRepository,
        action_repo: ActionRepository,
        event_repo: EventRepository,
    ):
        self.repository = repository
        self.action_repo = action_repo
        self.event_repo = event_repo
        self._chat_snapshots: OrderedDict[str, tuple[float, List[Chat]]] = OrderedDict()
        self._chat_list: Optional[tuple[float, List[Chat]]] = None
        self._chat_list_lock = asyncio.Lock()
        self._chat_list_generation = 0
        self._dirty_chat_ids: set[int] = set()
        self._chat_scan_retry_at = 0.0
        self._messages: OrderedDict[
            tuple[int, Optional[int]], tuple[float, List[Message]]
        ] = OrderedDict()
        self._message_locks: WeakValueDictionary[
            tuple[int, Optional[int]], asyncio.Lock
        ] = WeakValueDictionary()
        self._message_versions: OrderedDict[int, int] = OrderedDict()
        self._message_version_epoch = 0
        self._last_event_prefetch: OrderedDict[int, float] = OrderedDict()
        self._forum_topics: OrderedDict[int, tuple[float, ForumTopicPage]] = (
            OrderedDict()
        )
        self._forum_locks: WeakValueDictionary[int, asyncio.Lock] = (
            WeakValueDictionary()
        )
        self._prefetch_queue: asyncio.Queue[tuple[int, Chat, Optional[int]]] = (
            asyncio.Queue(maxsize=100)
        )
        self._prefetch_pending: set[tuple[int, int, Optional[int]]] = set()
        self._warmer_running = False

    async def initialize(self):
        await self.repository.connect()

    async def shutdown(self):
        await self.repository.disconnect()

    def replace_repository(self, repository: ChatRepository) -> None:
        self.repository = repository
        self._chat_snapshots.clear()
        self._dirty_chat_ids.clear()
        self._invalidate_chat_list()
        self._chat_scan_retry_at = 0.0
        self._messages.clear()
        self._forum_topics.clear()
        self._clear_message_versions()
        self._last_event_prefetch.clear()
        while not self._prefetch_queue.empty():
            self._prefetch_queue.get_nowait()
            self._prefetch_queue.task_done()
        self._prefetch_pending.clear()

    def _invalidate_chat_list(self) -> None:
        self._chat_list = None
        self._chat_list_generation += 1

    def _message_version(self, chat_id: int) -> tuple[int, int]:
        return self._message_version_epoch, self._message_versions.get(chat_id, 0)

    def _clear_message_versions(self) -> None:
        self._message_versions.clear()
        self._message_version_epoch += 1

    def _bump_message_version(self, chat_id: int) -> None:
        self._message_versions[chat_id] = self._message_versions.get(chat_id, 0) + 1
        self._message_versions.move_to_end(chat_id)
        while len(self._message_versions) > MAX_MESSAGE_VERSIONS:
            self._message_versions.popitem(last=False)
            # An evicted version may belong to an in-flight request. Changing
            # the epoch ensures eviction cannot make its old version look new.
            self._message_version_epoch += 1

    async def get_chat_page(
        self,
        snapshot: Optional[str] = None,
        offset: int = 0,
        refresh: bool = False,
        *,
        check_current: Optional[Callable[[], None]] = None,
    ) -> ChatPage:
        raise_if_task_stopped()
        if check_current is not None:
            check_current()
        now = time.monotonic()
        for key, (last_used, _) in list(self._chat_snapshots.items()):
            if now - last_used >= CHAT_SNAPSHOT_TTL:
                del self._chat_snapshots[key]

        if snapshot is None:
            chats = await self._get_chat_list(
                refresh=refresh, check_current=check_current
            )
            raise_if_task_stopped()
            if check_current is not None:
                check_current()
            # Telegram already orders pinned and recent chats; a stable sort
            # preserves that order within the unread and read groups.
            chats = sorted(chats, key=lambda chat: chat.unread_count <= 0)
            snapshot = secrets.token_urlsafe(18)
            while len(self._chat_snapshots) >= MAX_CHAT_SNAPSHOTS:
                self._chat_snapshots.popitem(last=False)
        else:
            cached = self._chat_snapshots.get(snapshot)
            if cached is None:
                raise ChatSnapshotExpired
            chats = cached[1]

        self._chat_snapshots[snapshot] = (time.monotonic(), chats)
        self._chat_snapshots.move_to_end(snapshot)
        end = offset + CHAT_PAGE_SIZE
        page_chats = chats[offset:end]
        self._queue_prefetch(page_chats)
        return ChatPage(page_chats, snapshot, end if end < len(chats) else None)

    async def _get_chat_list(
        self,
        refresh: bool = False,
        *,
        check_current: Optional[Callable[[], None]] = None,
    ) -> List[Chat]:
        def check() -> None:
            raise_if_task_stopped()
            if check_current is not None:
                check_current()

        check()
        # Serve the previous list while the background refresh is in flight.
        if self._warmer_running and not refresh and self._chat_list is not None:
            return self._chat_list[1]
        observed = self._chat_list
        while True:
            check()
            async with self._chat_list_lock:
                check()
                # Simultaneous explicit refreshes share the scan that finished
                # while they waited, rather than each fetching all dialogs.
                if (
                    refresh
                    and self._chat_list is not None
                    and self._chat_list is not observed
                ):
                    return self._chat_list[1]
                if (
                    refresh
                    or self._chat_list is None
                    or time.monotonic() - self._chat_list[0] >= CHAT_LIST_TTL
                ):
                    remaining = self._chat_scan_retry_at - time.monotonic()
                    if remaining > 0:
                        raise TelegramUnavailable(retry_after=math.ceil(remaining))
                    generation = self._chat_list_generation
                    repository = self.repository
                    try:
                        chats = await repository.get_chats()
                        check()
                        if generation != self._chat_list_generation:
                            continue
                        self._chat_list = (time.monotonic(), chats)
                        self._chat_scan_retry_at = 0.0
                    except AccountChanged:
                        check()
                        continue
                    except TelegramUnavailable as error:
                        check()
                        if repository is not self.repository:
                            continue
                        if error.retry_after is not None:
                            self._chat_scan_retry_at = max(
                                self._chat_scan_retry_at,
                                time.monotonic() + error.retry_after,
                            )
                        if generation != self._chat_list_generation:
                            continue
                        raise
                return self._chat_list[1]

    def _queue_prefetch(
        self, chats: List[Chat], topic_id: Optional[int] = None
    ) -> None:
        for chat in chats:
            key = (self._chat_list_generation, chat.id, topic_id)
            if key in self._prefetch_pending or self._prefetch_queue.full():
                continue
            self._prefetch_pending.add(key)
            self._prefetch_queue.put_nowait(
                (self._chat_list_generation, chat, topic_id)
            )

    def prefetch_topics(self, chat: Chat, topics: List[Chat]) -> None:
        for topic in topics:
            self._queue_prefetch([chat], topic_id=topic.id)

    async def _prefetch_worker(self) -> None:
        while True:
            generation, chat, topic_id = await self._prefetch_queue.get()
            try:
                if generation != self._chat_list_generation:
                    continue
                repository = self.repository
                if chat.image_url and topic_id is None:
                    try:
                        await repository.get_chat_avatar(chat.id)
                    except Exception as error:
                        if isinstance(error, TelegramUnavailable) and (
                            error.retry_after is not None
                        ):
                            raise
                        # An optional avatar failure must not skip history or
                        # topic prefetch, except for a surfaced flood wait.
                        logger.warning(
                            "chat_avatar_prefetch_failed",
                            chat_id=chat.id,
                            category=type(error).__name__,
                        )
                if generation != self._chat_list_generation:
                    continue
                if chat.type == ChatType.FORUM and topic_id is None:
                    topics = await self.get_forum_topics(chat.id)
                    if generation == self._chat_list_generation:
                        self.prefetch_topics(chat, topics[:5])
                else:
                    await self.get_chat_messages(chat.id, topic_id=topic_id)
            except Exception as error:
                logger.warning(
                    "chat_prefetch_failed",
                    chat_id=chat.id,
                    category=type(error).__name__,
                )
            except asyncio.CancelledError:
                # An old repository's shared download can be cancelled when an
                # account is switched without cancelling this worker itself.
                if asyncio.current_task().cancelling():
                    raise
            finally:
                self._prefetch_pending.discard((generation, chat.id, topic_id))
                self._prefetch_queue.task_done()

    async def run_cache_warmer(self, shutdown_event: asyncio.Event) -> None:
        self._warmer_running = True
        workers = [
            asyncio.create_task(self._prefetch_worker())
            for _ in range(PREFETCH_WORKERS)
        ]
        try:
            while not shutdown_event.is_set():
                try:
                    if self.repository.is_connected():
                        expired = (
                            self._chat_list is None
                            or time.monotonic() - self._chat_list[0] >= CHAT_LIST_TTL
                        )
                        chats = await self._get_chat_list(refresh=expired)
                        chats = sorted(chats, key=lambda chat: chat.unread_count <= 0)
                        self._queue_prefetch(chats[:CHAT_PAGE_SIZE])
                except Exception as error:
                    logger.warning(
                        "chat_cache_refresh_failed", category=type(error).__name__
                    )
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=CHAT_LIST_TTL)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._warmer_running = False
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def handle_event(self, event: SystemEvent) -> None:
        if event.type == "resync_required":
            self._invalidate_chat_list()
            self._chat_snapshots.clear()
            self._messages.clear()
            self._forum_topics.clear()
            self._dirty_chat_ids.clear()
            self._clear_message_versions()
            return
        if event.type == "profile_updated" and event.chat_id:
            peer_id = event.chat_id
            # An in-flight history may contain this sender even when no cached
            # history does. Cached unrelated histories remain available.
            self._message_version_epoch += 1
            if self._chat_list and any(
                chat.id == peer_id for chat in self._chat_list[1]
            ):
                if len(self._dirty_chat_ids) >= 1000:
                    self._invalidate_chat_list()
                    self._dirty_chat_ids.clear()
                self._dirty_chat_ids.add(peer_id)
            for key, (_, messages) in list(self._messages.items()):
                if any(
                    message.sender_id == peer_id
                    or any(
                        part.sender_id == peer_id for part in message.album_parts or ()
                    )
                    for message in messages
                ):
                    del self._messages[key]
            return
        if event.chat_id and event.type == "reaction_update" and event.message_model:
            # Reaction events contain only a message ID and reactions. Patch
            # those fields, never replace complete messages with the partial model.
            self._bump_message_version(event.chat_id)
            for key, (cached_at, messages) in list(self._messages.items()):
                if key[0] != event.chat_id:
                    continue
                updated = []
                changed = False
                for message in messages:
                    changes: dict[str, Any] = {}
                    if message.id == event.message_model.id:
                        changes["reactions"] = list(event.message_model.reactions)
                    if message.album_parts:
                        parts = [
                            replace(part, reactions=list(event.message_model.reactions))
                            if part.id == event.message_model.id
                            else part
                            for part in message.album_parts
                        ]
                        if any(a is not b for a, b in zip(parts, message.album_parts)):
                            changes["album_parts"] = parts
                    updated.append(replace(message, **changes) if changes else message)
                    changed |= bool(changes)
                if changed:
                    self._messages[key] = (cached_at, updated)
            return
        if event.chat_id and event.type in {
            "message",
            "edited",
            "deleted",
            "action",
            "read",
        }:
            if len(self._dirty_chat_ids) >= 1000:
                self._invalidate_chat_list()
                self._dirty_chat_ids.clear()
            self._dirty_chat_ids.add(event.chat_id)
        if event.chat_id and event.type == "read":
            self._forum_topics.pop(event.chat_id, None)
            self._bump_message_version(event.chat_id)
            if event.read_origin == "manual":
                await record_action(
                    self.action_repo,
                    ActionLog(
                        action="read_topic" if event.topic_id else "read_chat",
                        chat_id=event.chat_id,
                        chat_name=event.chat_name,
                        reason="manual",
                        link=event.link,
                    ),
                )
        if event.chat_id and event.type in {
            "message",
            "edited",
            "deleted",
            "reaction_update",
            "action",
        }:
            was_cached = any(key[0] == event.chat_id for key in self._messages)
            self._bump_message_version(event.chat_id)
            for key in list(self._messages):
                if key[0] == event.chat_id:
                    del self._messages[key]
            self._forum_topics.pop(event.chat_id, None)
            now = time.monotonic()
            if (
                was_cached
                and self._warmer_running
                and self._chat_list
                and now - self._last_event_prefetch.get(event.chat_id, float("-inf"))
                >= EVENT_PREFETCH_INTERVAL
            ):
                chat = next(
                    (chat for chat in self._chat_list[1] if chat.id == event.chat_id),
                    None,
                )
                if chat:
                    self._last_event_prefetch[event.chat_id] = now
                    self._last_event_prefetch.move_to_end(event.chat_id)
                    while len(self._last_event_prefetch) > MAX_EVENT_PREFETCH_TRACKED:
                        self._last_event_prefetch.popitem(last=False)
                    self._queue_prefetch([chat], topic_id=event.topic_id)

    async def get_chat(self, chat_id: int, *, refresh: bool = False) -> Optional[Chat]:
        raise_if_task_stopped()
        repository = self.repository
        self._ensure_repository_current(repository)
        if not refresh and chat_id not in self._dirty_chat_ids and self._chat_list:
            chat = next(
                (item for item in self._chat_list[1] if item.id == chat_id), None
            )
            if chat:
                raise_if_task_stopped()
                return chat
        for _ in range(3):
            self._ensure_repository_current(repository)
            generation = self._chat_list_generation
            version = self._message_version(chat_id)
            # A newer scan or card can finish while this lookup is in flight,
            # even if no Telegram event invalidates the chat.
            cached = self._chat_list
            cached_card = (
                next(
                    (
                        (index, item)
                        for index, item in enumerate(cached[1])
                        if item.id == chat_id
                    ),
                    None,
                )
                if cached is not None
                else None
            )

            def is_current() -> bool:
                return (
                    repository is self.repository
                    and generation == self._chat_list_generation
                    and version == self._message_version(chat_id)
                    and self._chat_list is cached
                    and (
                        cached_card is None
                        or (
                            cached is not None
                            and cached_card[0] < len(cached[1])
                            and cached[1][cached_card[0]] is cached_card[1]
                        )
                    )
                )

            try:
                chat = await repository.get_chat(chat_id)
            except Exception:
                self._ensure_repository_current(repository)
                raise
            self._ensure_repository_current(repository)
            if not is_current():
                continue
            if chat:
                if cached is not None and cached_card is not None:
                    # Pagination snapshots hold their own list and remain stable.
                    raise_if_task_stopped()
                    cached[1][cached_card[0]] = chat
                raise_if_task_stopped()
                self._dirty_chat_ids.discard(chat_id)
            raise_if_task_stopped()
            return chat
        raise TelegramUnavailable()

    async def get_recent_authors(self, chat_id: int) -> List[Dict[str, Any]]:
        repository = self.repository
        self._ensure_repository_current(repository)
        authors = await repository.get_recent_authors(chat_id)
        self._ensure_repository_current(repository)
        return authors

    async def get_forum_topics(self, chat_id: int) -> List[Chat]:
        return (await self.get_forum_topics_page(chat_id)).topics

    def _ensure_repository_current(self, repository: ChatRepository) -> None:
        raise_if_task_stopped()
        if self.repository is not repository or getattr(repository, "_retired", False):
            raise AccountChanged()

    async def get_forum_topics_page(
        self, chat_id: int, cursor: Optional[ForumTopicCursor] = None
    ) -> ForumTopicPage:
        repository = self.repository
        return await self._get_forum_topics_page(repository, chat_id, cursor)

    async def _get_forum_topics_page(
        self,
        repository: ChatRepository,
        chat_id: int,
        cursor: Optional[ForumTopicCursor],
    ) -> ForumTopicPage:
        self._ensure_repository_current(repository)
        if cursor is not None:
            version = self._message_version(chat_id)
            page = await repository.get_forum_topics_page(chat_id, cursor)
            self._ensure_repository_current(repository)
            if version != self._message_version(chat_id):
                raise TelegramUnavailable()
            return page
        lock = self._forum_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            retries = 0
            while True:
                self._ensure_repository_current(repository)
                cached = self._forum_topics.get(chat_id)
                if cached and time.monotonic() - cached[0] < (
                    CHAT_LIST_TTL if cached[1].topics else EMPTY_CACHE_TTL
                ):
                    self._forum_topics.move_to_end(chat_id)
                    self._ensure_repository_current(repository)
                    return cached[1]
                generation = self._chat_list_generation
                version = self._message_version(chat_id)
                topics = await repository.get_forum_topics_page(chat_id)
                self._ensure_repository_current(repository)
                if (
                    generation != self._chat_list_generation
                    or version != self._message_version(chat_id)
                ):
                    retries += 1
                    if retries < 3:
                        continue
                    raise TelegramUnavailable()
                self._forum_topics[chat_id] = (time.monotonic(), topics)
                self._forum_topics.move_to_end(chat_id)
                while len(self._forum_topics) > MAX_FORUM_TOPIC_CACHES:
                    self._forum_topics.popitem(last=False)
                self._ensure_repository_current(repository)
                return topics

    async def get_forum_topic(self, chat_id: int, topic_id: int) -> Optional[Chat]:
        repository = self.repository
        for _ in range(3):
            self._ensure_repository_current(repository)
            version = self._message_version(chat_id)
            topic = await repository.get_forum_topic(chat_id, topic_id)
            self._ensure_repository_current(repository)
            if version == self._message_version(chat_id):
                return topic
        raise TelegramUnavailable()

    async def get_message_by_id(self, chat_id: int, msg_id: int) -> Optional[Message]:
        repository = self.repository
        messages = await repository.get_messages(chat_id, ids=[msg_id])
        if repository is not self.repository:
            raise AccountChanged()
        if messages:
            return messages[0]
        return None

    async def get_single_message(self, chat_id: int, msg_id: int) -> Optional[Message]:
        # Alias for get_message_by_id
        return await self.get_message_by_id(chat_id, msg_id)

    async def _complete_album_boundary(
        self,
        repository: ChatRepository,
        chat_id: int,
        topic_id: Optional[int],
        messages: List[Message],
    ) -> List[Message]:
        """Include nearby album members without skipping interleaved messages."""
        self._ensure_repository_current(repository)
        if len(messages) < HISTORY_PAGE_SIZE:
            return messages
        boundary = messages[-1]
        groups = {
            message.grouped_id
            for index, message in enumerate(reversed(messages))
            if index < ALBUM_BOUNDARY_BAND and message.grouped_id
        }
        if not groups:
            return messages

        extra: list[Message] = []
        while len(extra) < MAX_ALBUM_BOUNDARY_EXTENSION:
            self._ensure_repository_current(repository)
            limit = min(
                ALBUM_BOUNDARY_LOOKAHEAD, MAX_ALBUM_BOUNDARY_EXTENSION - len(extra)
            )
            adjacent = await repository.get_messages(
                chat_id, limit=limit, topic_id=topic_id, offset_id=boundary.id
            )
            self._ensure_repository_current(repository)
            end = 0
            while True:
                self._ensure_repository_current(repository)
                matching = [
                    index + 1
                    for index, message in enumerate(adjacent)
                    if message.grouped_id in groups
                ]
                furthest = max(matching, default=0)
                if furthest <= end:
                    break
                groups.update(
                    message.grouped_id
                    for message in adjacent[end:furthest]
                    if message.grouped_id
                )
                end = furthest
            if not end:
                break
            extra.extend(adjacent[:end])
            boundary = extra[-1]
            if end < len(adjacent) or len(adjacent) < limit:
                break
        # shortcut: at most 40 extra messages and 20 between matching parts;
        # revisit if observed album spans exceed this bounded history lookahead.
        self._ensure_repository_current(repository)
        return [*messages, *extra]

    async def get_chat_messages(
        self,
        chat_id: int,
        topic_id: Optional[int] = None,
        offset_id: int = 0,
    ) -> List[Message]:
        repository = self.repository
        return await self._get_chat_messages(repository, chat_id, topic_id, offset_id)

    async def _get_chat_messages(
        self,
        repository: ChatRepository,
        chat_id: int,
        topic_id: Optional[int],
        offset_id: int,
    ) -> List[Message]:
        self._ensure_repository_current(repository)
        if offset_id:
            version = self._message_version(chat_id)
            raw = await repository.get_messages(
                chat_id, topic_id=topic_id, offset_id=offset_id
            )
            self._ensure_repository_current(repository)
            raw = await self._complete_album_boundary(
                repository, chat_id, topic_id, raw
            )
            self._ensure_repository_current(repository)
            if version != self._message_version(chat_id):
                raise TelegramUnavailable()
            messages = group_messages_into_albums(raw)
            self._ensure_repository_current(repository)
            return messages

        key = (chat_id, topic_id)
        lock = self._message_locks.setdefault(key, asyncio.Lock())
        async with lock:
            retries = 0
            while True:
                self._ensure_repository_current(repository)
                cached = self._messages.get(key)
                if cached and time.monotonic() - cached[0] < (
                    MESSAGE_CACHE_TTL if cached[1] else EMPTY_CACHE_TTL
                ):
                    self._messages.move_to_end(key)
                    self._ensure_repository_current(repository)
                    return cached[1]
                generation = self._chat_list_generation
                version = self._message_version(chat_id)
                raw = await repository.get_messages(
                    chat_id, topic_id=topic_id, offset_id=0
                )
                self._ensure_repository_current(repository)
                raw = await self._complete_album_boundary(
                    repository, chat_id, topic_id, raw
                )
                self._ensure_repository_current(repository)
                if generation != self._chat_list_generation:
                    continue
                messages = group_messages_into_albums(raw)
                self._ensure_repository_current(repository)
                if version != self._message_version(chat_id):
                    retries += 1
                    if retries < 3:
                        continue
                    # Do not present a snapshot known to have missed changes as
                    # an authoritative history (especially an empty chat).
                    raise TelegramUnavailable()
                self._messages[key] = (time.monotonic(), messages)
                self._messages.move_to_end(key)
                while len(self._messages) > MAX_MESSAGE_CACHES:
                    self._messages.popitem(last=False)
                self._ensure_repository_current(repository)
                return messages

    async def mark_chat_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int] = None,
        max_id: Optional[int] = None,
    ) -> None:
        await self.repository.mark_as_read(chat_id, topic_id, max_id=max_id)

    async def toggle_reaction(self, chat_id: int, msg_id: int, emoji: str) -> bool:
        return await self.repository.send_reaction(chat_id, msg_id, emoji)

    async def get_action_logs(self, limit: int = 50) -> List[ActionLog]:
        return await self.action_repo.get_logs(limit)

    async def get_media(
        self, chat_id: int, message_id: int, size_type: str = "preview"
    ) -> Optional[MediaContent]:
        return await self.repository.download_media(chat_id, message_id, size_type)

    async def get_chat_avatar(self, chat_id: int) -> Optional[bytes]:
        return await self.repository.get_chat_avatar(chat_id)

    async def get_recent_events(self, limit: int = 10) -> List[SystemEvent]:
        return await self.event_repo.get_recent_events(limit)
