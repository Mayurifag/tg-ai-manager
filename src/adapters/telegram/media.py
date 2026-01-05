import os
from typing import Optional, Any
from telethon import utils
from telethon.tl.types import (
    MessageMediaDocument,
    DocumentAttributeSticker,
    DocumentAttributeAudio,
    DocumentAttributeVideo,
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
                if filename.startswith("media_"):
                    continue

                file_path = os.path.join(self.images_dir, filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)
        except Exception as e:
            logger.error("startup_cache_cleanup_failed", error=str(e))

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
