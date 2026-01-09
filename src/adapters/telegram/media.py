import asyncio
import os
from typing import Any, Optional

from telethon import functions, utils
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    MessageMediaDocument,
)

from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class MediaMixin:
    def __init__(self):
        self.client: Any = None
        self.images_dir: str = ""

    def _get_avatar_path(self, chat_id: int) -> str:
        return os.path.join(self.images_dir, f"{chat_id}.jpg")

    def cleanup_startup_cache(self):
        """Cleans up avatar cache on startup."""
        if not os.path.exists(self.images_dir):
            return

        logger.info("cleaning_startup_cache")
        try:
            # We assume avatar files are numeric {chat_id}.jpg or similar,
            # while media files start with 'media_'.
            # We delete everything that doesn't start with media_.
            for filename in os.listdir(self.images_dir):
                if filename.startswith("media_") or filename.startswith("emoji_"):
                    continue

                file_path = os.path.join(self.images_dir, filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)
        except Exception as e:
            logger.error("startup_cache_cleanup_failed", error=str(e))

    async def run_storage_maintenance(self):
        """
        Enforces the disk usage limit for the cache directory.
        Deletes oldest files (by modification time) until total size is under limit.
        """
        if not self.images_dir or not os.path.exists(self.images_dir):
            return

        limit_mb = int(os.getenv("CACHE_MAX_SIZE_MB", "500"))
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
                        files.append((entry.path, stat.st_mtime, stat.st_size))

            if total_size <= limit_bytes:
                return

            # Sort by modification time (oldest first)
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
        """
        Refactored: Now returns the URL string if the entity has a photo,
        without checking disk or downloading.
        """
        if hasattr(entity, "photo") and entity.photo:
            if getattr(entity.photo, "photo_id", None) or getattr(
                entity.photo, "photo_small", None
            ):
                return f"/media/avatar/{chat_id}"
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
        Retrieves the avatar file path, downloading it if missing.
        No TTL check: cache is cleaned on app startup.
        """
        path = self._get_avatar_path(chat_id)

        if os.path.exists(path):
            return path

        try:
            # Try to resolve entity. If not found (e.g. unknown PeerUser), this raises ValueError.
            try:
                entity = await self.client.get_entity(chat_id)
            except ValueError:
                # Common for users not in session cache (lazy loading side effect)
                # We log this as debug to avoid spamming errors for every unknown user.
                logger.debug("avatar_entity_not_found", chat_id=chat_id)
                return None

            result = await self.client.download_profile_photo(
                entity, file=path, download_big=False
            )
            if result:
                return path
            return None
        except Exception as e:
            logger.error("avatar_download_failed", chat_id=chat_id, error=str(e))
            return None

    async def download_media(self, chat_id: int, message_id: int) -> Optional[str]:
        try:
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

            filename = f"media_{chat_id}_{message_id}.{ext}"
            path = os.path.join(self.images_dir, filename)

            if os.path.exists(path):
                return f"/cache/{filename}"

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
                result = await self.client.download_media(message, file=path)
            else:
                result = await self.client.download_media(message, file=path, thumb="m")
                if not result:
                    result = await self.client.download_media(message, file=path)

            if result:
                final_filename = os.path.basename(result)
                return f"/cache/{final_filename}"

        except Exception as e:
            logger.error(
                "download_media_failed",
                chat_id=chat_id,
                message_id=message_id,
                error=str(e),
            )
        return None

    async def get_custom_emoji_media(self, document_id: int) -> Optional[str]:
        """
        Downloads a custom emoji by its document ID.
        """
        filename = f"emoji_{document_id}.webp"  # Use webp/webm mostly
        path = os.path.join(self.images_dir, filename)

        # Check if already exists (any extension)
        # Simplified: Check specific likely extensions or just the ID prefix
        # For now, simplistic approach
        if os.path.exists(path):
            return f"/cache/{filename}"

        # Check for webm variant
        webm_path = path.replace(".webp", ".webm")
        if os.path.exists(webm_path):
            return f"/cache/{os.path.basename(webm_path)}"

        try:
            # Fetch the document info
            result = await self.client(
                functions.messages.GetCustomEmojiDocumentsRequest(
                    document_id=[document_id]
                )
            )

            if not result:
                return None

            document = result[0]

            # Determine extension
            ext = "webp"
            if document.mime_type == "video/webm":
                ext = "webm"
            elif document.mime_type == "application/x-tgsticker":
                ext = "tgs"

            final_path = os.path.join(self.images_dir, f"emoji_{document_id}.{ext}")

            await self.client.download_media(document, file=final_path)

            return f"/cache/{os.path.basename(final_path)}"

        except Exception as e:
            logger.error(
                "download_custom_emoji_failed", doc_id=document_id, error=str(e)
            )
            return None
