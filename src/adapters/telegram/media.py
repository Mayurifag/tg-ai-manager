import asyncio
import io
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional
from weakref import WeakSet

from telethon import errors, functions
from telethon.tl.types import (
    DocumentAttributeSticker,
    MessageMediaDocument,
    MessageMediaPhoto,
)

from src.adapters.telegram.query_errors import unavailable
from src.domain.errors import TelegramUnavailable
from src.domain.models import MediaContent
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)
MESSAGE_REFERENCE_TTL = 300
AVATAR_TTL = 24 * 60 * 60
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
MAX_ACTIVE_DOWNLOADS = 16
MAX_ACTIVE_STREAMS = 8
MEDIA_LOOKUP_ATTEMPTS = 2
# shortcut: release responses that never start; revisit if the server normally
# delays document bodies for more than 15 seconds.
STREAM_START_TIMEOUT = 15.0
# shortcut: track invalidations for 2,048 chats; revisit for larger active accounts.
MAX_MESSAGE_GENERATIONS = 2048
# shortcut: track avatar changes for 2,048 peers; invalidate in-flight
# observations on eviction. Revisit for sustained higher profile churn.
MAX_AVATAR_GENERATIONS = 2048


@dataclass(eq=False, frozen=True)
class AvatarGeneration:
    epoch: int
    revision: int


class MediaTooLarge(ValueError):
    """An in-memory preview exceeded the per-download budget."""


class MediaBusy(RuntimeError):
    """Media cannot be served reliably right now; try again."""


class _BoundedBytesIO(io.BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_DOWNLOAD_BYTES:
            raise MediaTooLarge("Media exceeds the in-memory download limit")
        return super().write(data)


async def _download_bytes(download, *args, **kwargs) -> Optional[bytes]:
    with _BoundedBytesIO() as output:
        result = await download(*args, file=output, **kwargs)
        return output.getvalue() if result is not None else None


class _StreamLease:
    """Own a stream slot even if the response is closed before iteration."""

    def __init__(self, iterator: AsyncIterator[bytes], release) -> None:
        self._iterator = iterator
        self._release = release
        self._closed = False
        self._driver: Optional[asyncio.Task] = None
        self._start_timeout = asyncio.get_running_loop().call_later(
            STREAM_START_TIMEOUT, self._expire_unstarted
        )

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        self._start_timeout.cancel()
        if self._closed:
            raise StopAsyncIteration
        self._driver = asyncio.current_task()
        try:
            return await self._iterator.__anext__()
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._start_timeout.cancel()
        try:
            await self._iterator.aclose()
        finally:
            self._release(self)

    def _expire_unstarted(self) -> None:
        if not self._closed:
            # iter_document has not run yet, so it has acquired no resources.
            self._closed = True
            self._release(self)

    async def cancel(self, owner: Optional[asyncio.Task] = None) -> None:
        driver = self._driver
        if (
            driver
            and not driver.done()
            and driver is not asyncio.current_task()
            and driver is not owner
        ):
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
        if not self._closed:
            await self.aclose()


class MediaManager:
    """Bounded, process-local preview cache; no media is written to disk."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self._limit = max(1, int(os.getenv("CACHE_MAX_SIZE_MB", "128"))) * 1024 * 1024
        self._cache: OrderedDict[tuple, tuple[float, MediaContent]] = OrderedDict()
        self._cache_size = 0
        self._messages: OrderedDict[tuple[int, int], tuple[float, Any]] = OrderedDict()
        self._message_generations: OrderedDict[int, int] = OrderedDict()
        self._message_generation_epoch = 0
        self._avatar_entities: OrderedDict[int, tuple[Any, Any]] = OrderedDict()
        self._avatar_generations: OrderedDict[int, int] = OrderedDict()
        self._avatar_generation_epoch = 0
        self._avatar_revision = 0
        self._active_avatar_generations: WeakSet[AvatarGeneration] = WeakSet()
        self._downloads: dict[tuple, asyncio.Task[Optional[MediaContent]]] = {}
        self._download_tasks: set[asyncio.Task[Optional[MediaContent]]] = set()
        self._preview_queue: asyncio.Queue[tuple[int, Optional[int]]] = asyncio.Queue(
            maxsize=1000
        )
        self._preview_pending: set[tuple[int, Optional[int]]] = set()
        self._preview_workers: set[asyncio.Task[None]] = set()
        self._stream_leases: set[_StreamLease] = set()
        self._closed = False

    def _get(self, key: tuple) -> Optional[MediaContent]:
        cached = self._cache.get(key)
        if cached is None:
            return None
        if key[0] == "avatar" and time.monotonic() - cached[0] >= AVATAR_TTL:
            self._invalidate(key)
            return None
        self._cache.move_to_end(key)
        return cached[1]

    def _put(self, key: tuple, item: MediaContent) -> None:
        if item.data is None or len(item.data) > self._limit:
            return
        previous = self._cache.pop(key, None)
        if previous and previous[1].data is not None:
            self._cache_size -= len(previous[1].data)
        while self._cache and self._cache_size + len(item.data) > self._limit:
            _, old = self._cache.popitem(last=False)
            self._cache_size -= len(old[1].data or b"")
        self._cache[key] = (time.monotonic(), item)
        self._cache_size += len(item.data)

    async def _shared(self, key: tuple, download) -> Optional[MediaContent]:
        if self._closed:
            raise RuntimeError("Media manager is closed")
        cached = self._get(key)
        if cached is not None:
            return cached
        task = self._downloads.get(key)
        if task is None:
            if len(self._download_tasks) >= MAX_ACTIVE_DOWNLOADS:
                raise MediaBusy("Too many media downloads")

            async def run() -> Optional[MediaContent]:
                item = await download()
                if (
                    item
                    and not self._closed
                    and self._downloads.get(key) is asyncio.current_task()
                ):
                    self._put(key, item)
                return item

            def finished(completed: asyncio.Task[Optional[MediaContent]]) -> None:
                if not completed.cancelled():
                    completed.exception()  # A shielded caller may have disconnected.
                self._download_tasks.discard(completed)
                if self._downloads.get(key) is completed:
                    self._downloads.pop(key, None)

            task = asyncio.create_task(run())
            self._downloads[key] = task
            self._download_tasks.add(task)
            task.add_done_callback(finished)
        item = await asyncio.shield(task)
        return None if self._closed else item

    def _invalidate(self, key: tuple) -> None:
        item = self._cache.pop(key, None)
        if item and item[1].data is not None:
            self._cache_size -= len(item[1].data)
        # Let existing callers finish, but never reuse or cache an obsolete download.
        self._downloads.pop(key, None)

    def avatar_generation(self) -> AvatarGeneration:
        self._avatar_revision += 1
        generation = AvatarGeneration(
            self._avatar_generation_epoch, self._avatar_revision
        )
        if not self._closed:
            self._active_avatar_generations.add(generation)
        return generation

    def _remember_avatar_generation(self, chat_id: int, revision: int) -> None:
        self._avatar_generations[chat_id] = revision
        self._avatar_generations.move_to_end(chat_id)
        while len(self._avatar_generations) > MAX_AVATAR_GENERATIONS:
            self._avatar_generations.popitem(last=False)
            self._avatar_generation_epoch += 1

    def _bump_avatar_generation(self, chat_id: int) -> None:
        self._avatar_revision += 1
        self._remember_avatar_generation(chat_id, self._avatar_revision)

    async def _get_chat_image(
        self,
        entity: Any,
        chat_id: int,
        *,
        generation: Optional[AvatarGeneration] = None,
    ) -> Optional[str]:
        if self._closed:
            return None
        if generation is None:
            generation = self.avatar_generation()
        key = ("avatar", chat_id)
        if (
            generation.epoch != self._avatar_generation_epoch
            or self._avatar_generations.get(chat_id, 0) > generation.revision
        ):
            # A later observation or photo event owns the current avatar.
            if chat_id in self._avatar_entities or self._get(key) is not None:
                return f"/media/avatar/{chat_id}"
            return None
        # Even a first observation (including no photo) supersedes an older
        # in-flight lookup. Avoid tracking absent photos when nothing older is
        # still in flight, especially during a large dialog scan.
        if any(
            other.epoch == generation.epoch and other.revision < generation.revision
            for other in self._active_avatar_generations
        ):
            self._remember_avatar_generation(chat_id, generation.revision)
        photo = getattr(entity, "photo", None)
        photo_id = getattr(photo, "photo_id", None)
        if photo and (photo_id or getattr(photo, "photo_small", None)):
            previous = self._avatar_entities.get(chat_id)
            if previous is None or not photo_id or previous[0] != photo_id:
                if previous is not None or key in self._cache or key in self._downloads:
                    self._bump_avatar_generation(chat_id)
                self._invalidate(key)
            self._avatar_entities[chat_id] = (photo_id, entity)
            self._avatar_entities.move_to_end(chat_id)
            while len(self._avatar_entities) > 1000:
                self._avatar_entities.popitem(last=False)
            return f"/media/avatar/{chat_id}"
        if (
            chat_id in self._avatar_entities
            or key in self._cache
            or key in self._downloads
        ):
            self.clear_chat_avatar(chat_id)
        return None

    def clear_chat_avatar(self, chat_id: int) -> None:
        if self._closed:
            return
        self._bump_avatar_generation(chat_id)
        self._invalidate(("avatar", chat_id))
        self._avatar_entities.pop(chat_id, None)

    def invalidate_message(self, chat_id: int, message_id: int) -> None:
        if self._closed:
            return
        # In-flight Telegram lookups may still return the pre-edit reference.
        self._message_generations[chat_id] = (
            self._message_generations.get(chat_id, 0) + 1
        )
        self._message_generations.move_to_end(chat_id)
        while len(self._message_generations) > MAX_MESSAGE_GENERATIONS:
            self._message_generations.popitem(last=False)
            # An evicted generation may still be observed by an in-flight lookup.
            self._message_generation_epoch += 1
        self._messages.pop((chat_id, message_id), None)
        for size_type in ("preview", "full"):
            self._invalidate(("media", chat_id, message_id, size_type))

    def invalidate_all_messages(self) -> None:
        """Discard message media when a deletion cannot be attributed to a chat."""
        if self._closed:
            return
        self._message_generation_epoch += 1
        self._message_generations.clear()
        self._messages.clear()
        for key in set(self._cache) | set(self._downloads):
            if key[0] == "media":
                self._invalidate(key)

    def message_generation(self, chat_id: int) -> tuple[int, int]:
        return self._message_generation_epoch, self._message_generations.get(chat_id, 0)

    def remember_messages(self, chat_id: int, messages: list[Any]) -> None:
        if self._closed:
            return
        for message in messages:
            if message and getattr(message, "media", None):
                key = (chat_id, message.id)
                self._messages[key] = (time.monotonic(), message)
                self._messages.move_to_end(key)
        while len(self._messages) > 1000:
            self._messages.popitem(last=False)

    async def _message(
        self, chat_id: int, message_id: int, *, refresh: bool = False
    ) -> Any:
        if self._closed:
            return None
        key = (chat_id, message_id)
        cached = self._messages.get(key)
        if (
            cached
            and not refresh
            and time.monotonic() - cached[0] < MESSAGE_REFERENCE_TTL
        ):
            self._messages.move_to_end(key)
            return cached[1]
        self._messages.pop(key, None)
        # shortcut: retry one invalidated lookup; revisit if busy chats routinely fail.
        for _ in range(MEDIA_LOOKUP_ATTEMPTS):
            generation = self.message_generation(chat_id)
            entity = await self.client.get_entity(chat_id)
            messages = await self.client.get_messages(entity, ids=[message_id])
            if self._closed:
                return None
            if generation != self.message_generation(chat_id):
                # Prefer the reference from the edit event, if it supplied one.
                cached = self._messages.get(key)
                if (
                    not refresh
                    and cached
                    and time.monotonic() - cached[0] < MESSAGE_REFERENCE_TTL
                ):
                    self._messages.move_to_end(key)
                    return cached[1]
                continue
            if not messages or not messages[0]:
                return None
            self.remember_messages(chat_id, messages)
            return messages[0]
        # Do not report absence or return a reference known to predate an edit.
        raise MediaBusy("Message changed during lookup")

    async def get_chat_avatar(self, chat_id: int) -> Optional[bytes]:
        key = ("avatar", chat_id)

        async def download() -> Optional[MediaContent]:
            try:
                cached_entity = self._avatar_entities.get(chat_id)
                entity = (
                    cached_entity[1]
                    if cached_entity is not None
                    else await self.client.get_entity(chat_id)
                )
                data = await _download_bytes(
                    self.client.download_profile_photo, entity, download_big=False
                )
                if isinstance(data, bytes) and data:
                    return MediaContent(data=data, mime_type="image/jpeg")
            except (MediaBusy, MediaTooLarge, TelegramUnavailable):
                raise
            except Exception as error:
                logger.warning(
                    "avatar_download_failed",
                    chat_id=chat_id,
                    category=type(error).__name__,
                )
                raise unavailable(error) from error
            return None

        for _ in range(MEDIA_LOOKUP_ATTEMPTS):
            if self._closed:
                return None
            generation = self.avatar_generation()
            item = await self._shared(key, download)
            if self._closed:
                return None
            if (
                generation.epoch == self._avatar_generation_epoch
                and self._avatar_generations.get(chat_id, 0) <= generation.revision
            ):
                return item.data if item else None
        raise MediaBusy("Avatar changed during download")

    def prefetch_previews(
        self,
        chat_id: int,
        messages: list[Any],
        parsed: list[Any],
        *,
        generation: tuple[int, int],
    ) -> None:
        if self._closed or generation != self.message_generation(chat_id):
            return
        self.remember_messages(chat_id, messages)
        for message in messages:
            media = getattr(message, "media", None)
            sticker = (
                isinstance(media, MessageMediaDocument)
                and getattr(media.document, "mime_type", "") == "image/webp"
                and any(
                    isinstance(attr, DocumentAttributeSticker)
                    for attr in getattr(media.document, "attributes", [])
                )
            )
            if not isinstance(media, MessageMediaPhoto) and not sticker:
                continue
            key = (chat_id, message.id)
            if key not in self._preview_pending and not self._preview_queue.full():
                self._preview_pending.add(key)
                self._preview_queue.put_nowait((chat_id, message.id))
        for message in parsed:
            key = (message.sender_id, None)
            if (
                message.avatar_url
                and message.sender_id
                and key not in self._preview_pending
                and not self._preview_queue.full()
            ):
                self._preview_pending.add(key)
                self._preview_queue.put_nowait((message.sender_id, None))
        self._preview_workers.difference_update(
            {task for task in self._preview_workers if task.done()}
        )
        while not self._preview_queue.empty() and len(self._preview_workers) < 3:
            task = asyncio.create_task(self._preview_worker())
            self._preview_workers.add(task)
            task.add_done_callback(self._preview_workers.discard)

    async def _preview_worker(self) -> None:
        while not self._preview_queue.empty():
            chat_id, message_id = self._preview_queue.get_nowait()
            try:
                if message_id is None:
                    await self.get_chat_avatar(chat_id)
                else:
                    await self.download_media(chat_id, message_id)
            except (MediaBusy, MediaTooLarge):
                logger.info("preview_prefetch_skipped", chat_id=chat_id)
            except Exception as error:
                logger.warning(
                    "preview_prefetch_failed",
                    chat_id=chat_id,
                    category=type(error).__name__,
                )
            finally:
                self._preview_pending.discard((chat_id, message_id))
                self._preview_queue.task_done()

    async def download_media(
        self,
        chat_id: int,
        message_id: int,
        size_type: str = "preview",
    ) -> Optional[MediaContent]:
        key = ("media", chat_id, message_id, size_type)

        async def download() -> Optional[MediaContent]:
            try:
                current = await self._message(chat_id, message_id)
                media = getattr(current, "media", None)
                if not media:
                    return None
                if isinstance(media, MessageMediaDocument):
                    document = media.document
                    sticker = any(
                        isinstance(attr, DocumentAttributeSticker)
                        for attr in getattr(document, "attributes", [])
                    )
                    if not sticker:
                        return MediaContent(
                            data=None,
                            mime_type=getattr(document, "mime_type", None)
                            or "application/octet-stream",
                        )
                    mime_type = (
                        getattr(document, "mime_type", None)
                        or "application/octet-stream"
                    )
                    data = await _download_bytes(self.client.download_media, current)
                else:
                    mime_type = "image/jpeg"
                    if size_type == "full":
                        data = await _download_bytes(
                            self.client.download_media, current
                        )
                    else:
                        data = None
                        for thumb in ("m", "x", "s", 0):
                            data = await _download_bytes(
                                self.client.download_media, current, thumb=thumb
                            )
                            if data:
                                break
                if isinstance(data, bytes) and data:
                    return MediaContent(data=data, mime_type=mime_type)
            except (MediaBusy, MediaTooLarge, TelegramUnavailable):
                raise
            except Exception as error:
                logger.warning(
                    "download_media_failed",
                    chat_id=chat_id,
                    message_id=message_id,
                    category=type(error).__name__,
                )
                raise unavailable(error) from error
            return None

        for _ in range(MEDIA_LOOKUP_ATTEMPTS):
            if self._closed:
                return None
            generation = self.message_generation(chat_id)
            item = await self._shared(key, download)
            if self._closed:
                return None
            if generation == self.message_generation(chat_id):
                return item
        raise MediaBusy("Message changed during download")

    async def get_document(self, chat_id: int, message_id: int) -> Any:
        try:
            message = await self._message(chat_id, message_id)
            media = getattr(message, "media", None)
            return media.document if isinstance(media, MessageMediaDocument) else None
        except (MediaBusy, MediaTooLarge, TelegramUnavailable):
            raise
        except Exception as error:
            logger.warning(
                "document_lookup_failed",
                chat_id=chat_id,
                message_id=message_id,
                category=type(error).__name__,
            )
            raise unavailable(error) from error

    async def iter_document(
        self, chat_id: int, message_id: int, document: Any, start: int, length: int
    ) -> AsyncIterator[bytes]:
        remaining = length
        offset = start
        for attempt in range(2):
            if not remaining:
                return
            iterator = self.client.iter_download(
                document,
                offset=offset,
                chunk_size=65536,
                request_size=524288,
                file_size=document.size,
            )
            try:
                async for chunk in iterator:
                    data = bytes(chunk[:remaining])
                    remaining -= len(data)
                    offset += len(data)
                    yield data
                    if not remaining:
                        return
                raise IOError("Telegram document stream ended early")
            except (
                errors.FileReferenceExpiredError,
                errors.FileReferenceEmptyError,
                errors.FileReferenceInvalidError,
                errors.FilerefUpgradeNeededError,
            ):
                if attempt:
                    raise
                message = await self._message(chat_id, message_id, refresh=True)
                media = getattr(message, "media", None)
                replacement = (
                    media.document if isinstance(media, MessageMediaDocument) else None
                )
                if not replacement or replacement.id != document.id:
                    raise
                document = replacement
            finally:
                # Telethon's iterator has no _sender until its first iteration.
                if getattr(iterator, "_sender", None):
                    await iterator.close()

    def open_document_stream(
        self, chat_id: int, message_id: int, document: Any, start: int, length: int
    ) -> _StreamLease:
        if self._closed:
            raise RuntimeError("Media manager is closed")
        if len(self._stream_leases) >= MAX_ACTIVE_STREAMS:
            raise MediaBusy("Too many document streams")
        lease = _StreamLease(
            self.iter_document(chat_id, message_id, document, start, length),
            self._stream_leases.discard,
        )
        self._stream_leases.add(lease)
        return lease

    async def get_custom_emoji_media(self, document_id: int) -> Optional[MediaContent]:
        key = ("emoji", document_id)

        async def download() -> Optional[MediaContent]:
            try:
                documents = await self.client(
                    functions.messages.GetCustomEmojiDocumentsRequest(
                        document_id=[document_id]
                    )
                )
                if documents:
                    document = documents[0]
                    data = await _download_bytes(self.client.download_media, document)
                    if isinstance(data, bytes) and data:
                        return MediaContent(
                            data=data,
                            mime_type=document.mime_type or "application/octet-stream",
                        )
            except (MediaBusy, MediaTooLarge, TelegramUnavailable):
                raise
            except Exception as error:
                logger.warning(
                    "download_custom_emoji_failed",
                    doc_id=document_id,
                    category=type(error).__name__,
                )
                raise unavailable(error) from error
            return None

        return await self._shared(key, download)

    async def close(self) -> None:
        self._closed = True
        owner = asyncio.current_task()
        tasks = [
            task
            for task in (*self._preview_workers, *self._download_tasks)
            if task is not owner
        ]
        # Cancel independent work before waiting on stream teardown: a stalled
        # lease must not leave other downloads running when close is cancelled.
        for task in tasks:
            task.cancel()
        results: list[Any] = []
        try:
            if self._stream_leases:
                results = await asyncio.gather(
                    *(lease.cancel(owner) for lease in tuple(self._stream_leases)),
                    return_exceptions=True,
                )
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._downloads.clear()
            self._cache.clear()
            self._messages.clear()
            self._message_generations.clear()
            self._message_generation_epoch += 1
            self._avatar_entities.clear()
            self._avatar_generations.clear()
            self._active_avatar_generations.clear()
            self._avatar_generation_epoch += 1
            while not self._preview_queue.empty():
                self._preview_queue.get_nowait()
                self._preview_queue.task_done()
            self._preview_pending.clear()
            self._cache_size = 0
        for result in results:
            if isinstance(result, BaseException):
                raise result
