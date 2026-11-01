import asyncio
import glob
import os
import time
from typing import Any, Optional

from telethon import functions, utils
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
)

from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class MediaManager:
    def __init__(self, client: Any, images_dir: str) -> None:
        self.client = client
        self.base_dir = images_dir
        self.images_dir = images_dir
        self.account_id: Optional[int] = None
        self._downloads: dict[tuple[int, int, str], asyncio.Task[Optional[str]]] = {}
        self._avatars: dict[int, asyncio.Task[Optional[str]]] = {}
        self._preview_queue: asyncio.Queue[tuple[int, Any]] = asyncio.Queue(
            maxsize=1000
        )
        self._preview_pending: set[tuple[int, Optional[int]]] = set()
        self._queued_messages: dict[tuple[int, int], Any] = {}
        self._preview_workers: set[asyncio.Task[None]] = set()

    def set_account(self, account_id: int) -> None:
        self.account_id = account_id
        self.images_dir = os.path.join(self.base_dir, str(account_id))
        os.makedirs(self.images_dir, exist_ok=True)

    def _cache_url(self, path: str) -> str:
        return "/cache/" + os.path.relpath(path, self.base_dir).replace(os.sep, "/")

    def _get_avatar_path(self, chat_id: int) -> str:
        return os.path.join(self.images_dir, f"{chat_id}.jpg")

    async def run_storage_maintenance(self):
        """
        Enforces the disk usage limit for the cache directory.
        Deletes oldest files (by modification time) until total size is under limit.
        """
        if not self.images_dir or not os.path.exists(self.images_dir):
            return

        limit_mb = int(os.getenv("CACHE_MAX_SIZE_MB", "1024"))
        limit_bytes = limit_mb * 1024 * 1024

        await asyncio.to_thread(self._cleanup_sync, limit_bytes)

    def _cleanup_sync(self, limit_bytes: int):
        try:
            files = []
            total_size = 0

            with os.scandir(self.images_dir) as entries:
                for entry in entries:
                    if entry.is_file():
                        stat = entry.stat()
                        total_size += stat.st_size
                        if not entry.name.endswith(".part"):
                            files.append((entry.path, stat.st_mtime, stat.st_size))

            if total_size <= limit_bytes:
                return

            files.sort(key=lambda x: x[1])

            deleted_count = 0
            freed_bytes = 0

            for path, _, size in files:
                if total_size <= limit_bytes:
                    break
                try:
                    os.remove(path)
                    total_size -= size
                    freed_bytes += size
                    deleted_count += 1
                except OSError as e:
                    logger.error("cache_delete_failed", path=path, error=str(e))

            if deleted_count > 0:
                logger.info(
                    "cache_maintenance_completed",
                    deleted_files=deleted_count,
                    freed_mb=round(freed_bytes / (1024 * 1024), 2),
                )

        except Exception as e:
            logger.error("cache_maintenance_failed", error=str(e))

    async def _get_chat_image(self, entity: Any, chat_id: int) -> Optional[str]:
        if hasattr(entity, "photo") and entity.photo:
            if getattr(entity.photo, "photo_id", None) or getattr(
                entity.photo, "photo_small", None
            ):
                account = (
                    f"?account={self.account_id}" if self.account_id is not None else ""
                )
                return f"/media/avatar/{chat_id}{account}"
        return None

    def clear_chat_avatar(self, chat_id: int):
        """Removes the cached avatar to force re-download."""
        path = self._get_avatar_path(chat_id)
        if os.path.exists(path):
            try:
                os.remove(path)
                logger.info("avatar_cache_cleared", chat_id=chat_id)
            except OSError as e:
                logger.error("avatar_clear_failed", chat_id=chat_id, error=str(e))

    async def get_chat_avatar(self, chat_id: int) -> Optional[str]:
        """
        Retrieves a cached avatar, refreshing it at most once per day.
        """
        task = self._avatars.get(chat_id)
        if task is None:
            task = asyncio.create_task(self._download_avatar(chat_id))
            self._avatars[chat_id] = task
            task.add_done_callback(lambda _: self._avatars.pop(chat_id, None))
        return await asyncio.shield(task)

    async def _download_avatar(self, chat_id: int) -> Optional[str]:
        path = self._get_avatar_path(chat_id)
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400:
            return path

        temporary_path = path + ".part"
        try:
            try:
                entity = await self.client.get_entity(chat_id)
            except ValueError:
                logger.debug("avatar_entity_not_found", chat_id=chat_id)
                return path if os.path.exists(path) else None

            result = await self.client.download_profile_photo(
                entity, file=temporary_path, download_big=False
            )
            if result:
                os.replace(result, path)
                return path
            return path if os.path.exists(path) else None
        except Exception as e:
            logger.error("avatar_download_failed", chat_id=chat_id, error=str(e))
            return path if os.path.exists(path) else None
        finally:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)

    def prefetch_previews(
        self, chat_id: int, messages: list[Any], parsed: list[Any]
    ) -> None:
        """Warm lightweight image previews, never whole videos or documents."""
        for message in messages:
            media = getattr(message, "media", None)
            sticker = (
                isinstance(media, MessageMediaDocument)
                and any(
                    isinstance(attr, DocumentAttributeSticker)
                    for attr in getattr(media.document, "attributes", [])
                )
                and getattr(media.document, "mime_type", "") == "image/webp"
            )
            if not isinstance(media, MessageMediaPhoto) and not sticker:
                continue
            key = (chat_id, message.id)
            if key in self._preview_pending or self._preview_queue.full():
                continue
            self._preview_pending.add(key)
            self._queued_messages[key] = message
            self._preview_queue.put_nowait((chat_id, message))
        for message in parsed:
            key = (message.sender_id, None)
            if (
                not message.avatar_url
                or not message.sender_id
                or key in self._preview_pending
                or self._preview_queue.full()
            ):
                continue
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
            chat_id, message = self._preview_queue.get_nowait()
            try:
                if message is None:
                    await self.get_chat_avatar(chat_id)
                else:
                    await self.download_media(chat_id, message.id, message=message)
            finally:
                self._preview_pending.discard(
                    (chat_id, message.id if message else None)
                )
                if message:
                    self._queued_messages.pop((chat_id, message.id), None)
                self._preview_queue.task_done()

    async def close(self) -> None:
        tasks = [
            *self._preview_workers,
            *self._downloads.values(),
            *self._avatars.values(),
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def download_media(
        self,
        chat_id: int,
        message_id: int,
        size_type: str = "preview",
        *,
        message: Any = None,
    ) -> Optional[str]:
        key = (chat_id, message_id, size_type)
        task = self._downloads.get(key)
        if task is None:
            if message is None and size_type == "preview":
                message = self._queued_messages.get((chat_id, message_id))
            task = asyncio.create_task(
                self._download_media(chat_id, message_id, size_type, message)
            )
            self._downloads[key] = task
            task.add_done_callback(lambda _: self._downloads.pop(key, None))
        # A disconnected browser must not cancel a download shared by another request.
        return await asyncio.shield(task)

    async def _download_media(
        self, chat_id: int, message_id: int, size_type: str, message: Any = None
    ) -> Optional[str]:
        try:
            suffix = "_full" if size_type == "full" else ""
            pattern = os.path.join(
                self.images_dir, f"media_{chat_id}_{message_id}{suffix}.*"
            )
            cached = next(
                (
                    path
                    for path in glob.iglob(pattern)
                    if os.path.isfile(path) and not path.endswith(".part")
                ),
                None,
            )
            if cached:
                return self._cache_url(cached)

            if message is None:
                entity = await self.client.get_entity(chat_id)
                messages = await self.client.get_messages(entity, ids=[message_id])
                if not messages:
                    return None
                message = messages[0]

            if not message or not getattr(message, "media", None):
                return None

            ext = "jpg"
            guessed_ext = utils.get_extension(message.media)
            if guessed_ext:
                ext = guessed_ext.lstrip(".")

            if hasattr(message.media, "document"):
                if hasattr(message.media.document, "mime_type"):
                    if "webp" in message.media.document.mime_type:
                        ext = "webp"
                    elif "audio/ogg" in message.media.document.mime_type:
                        ext = "ogg"
                    elif "audio/mpeg" in message.media.document.mime_type:
                        ext = "mp3"
                    elif "video/mp4" in message.media.document.mime_type:
                        ext = "mp4"

            filename = f"media_{chat_id}_{message_id}{suffix}.{ext}"
            path = os.path.join(self.images_dir, filename)
            temporary_path = path + ".part"

            if os.path.exists(path):
                return self._cache_url(path)

            is_sticker = False
            is_audio = False
            is_video = False

            if isinstance(message.media, MessageMediaDocument):
                for attr in getattr(message.media.document, "attributes", []):
                    if isinstance(attr, DocumentAttributeSticker):
                        is_sticker = True
                    if isinstance(attr, DocumentAttributeAudio):
                        is_audio = True
                    if isinstance(attr, DocumentAttributeVideo):
                        is_video = True

            result = None

            if is_sticker or is_audio or is_video:
                result = await self.client.download_media(message, file=temporary_path)
            else:
                if size_type == "full":
                    result = await self.client.download_media(
                        message, file=temporary_path, thumb=None
                    )
                else:
                    result = await self.client.download_media(
                        message, file=temporary_path, thumb="m"
                    )
                    if not result:
                        result = await self.client.download_media(
                            message, file=temporary_path
                        )

            if result:
                os.replace(result, path)
                return self._cache_url(path)

        except Exception as e:
            logger.error(
                "download_media_failed",
                chat_id=chat_id,
                message_id=message_id,
                error=str(e),
            )
        finally:
            if "temporary_path" in locals() and os.path.exists(temporary_path):
                os.remove(temporary_path)
        return None

    async def get_custom_emoji_media(self, document_id: int) -> Optional[str]:
        filename = f"emoji_{document_id}.webp"
        path = os.path.join(self.images_dir, filename)

        if os.path.exists(path):
            return self._cache_url(path)

        webm_path = path.replace(".webp", ".webm")
        if os.path.exists(webm_path):
            return self._cache_url(webm_path)

        try:
            result = await self.client(
                functions.messages.GetCustomEmojiDocumentsRequest(
                    document_id=[document_id]
                )
            )

            if not result:
                return None

            document = result[0]

            ext = "webp"
            if document.mime_type == "video/webm":
                ext = "webm"
            elif document.mime_type == "application/x-tgsticker":
                ext = "tgs"

            final_path = os.path.join(self.images_dir, f"emoji_{document_id}.{ext}")

            await self.client.download_media(document, file=final_path)

            return self._cache_url(final_path)

        except Exception as e:
            logger.error(
                "download_custom_emoji_failed", doc_id=document_id, error=str(e)
            )
            return None
