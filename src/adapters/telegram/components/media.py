import gzip
import os
import shutil
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


class TelegramMediaComponent:
    def __init__(self, client: Any, cache_dir: str):
        self.client = client
        self.images_dir = cache_dir
        os.makedirs(self.images_dir, exist_ok=True)

    def _get_avatar_path(self, chat_id: int) -> str:
        return os.path.join(self.images_dir, f"{chat_id}.jpg")

    def _decompress_tgs(self, tgs_path: str) -> Optional[str]:
        """Decompresses a .tgs file to a .json lottie file."""
        json_path = tgs_path.replace(".tgs", ".json")
        try:
            with gzip.open(tgs_path, "rb") as f_in:
                with open(json_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            return json_path
        except Exception as e:
            logger.error("tgs_decompression_failed", path=tgs_path, error=str(e))
            return None

    async def get_chat_image_url(self, entity: Any, chat_id: int) -> Optional[str]:
        if hasattr(entity, "photo") and entity.photo:
            if getattr(entity.photo, "photo_id", None) or getattr(
                entity.photo, "photo_small", None
            ):
                return f"/media/avatar/{chat_id}"
        return None

    async def download_avatar(self, chat_id: int) -> Optional[str]:
        path = self._get_avatar_path(chat_id)
        if os.path.exists(path):
            return path

        try:
            entity = await self.client.get_entity(chat_id)
            result = await self.client.download_profile_photo(
                entity, file=path, download_big=False
            )
            if result:
                return path
            return None
        except Exception as e:
            logger.error("avatar_download_failed", chat_id=chat_id, error=str(e))
            return None

    def clear_avatar_cache(self, chat_id: int):
        path = self._get_avatar_path(chat_id)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    async def download_message_media(
        self, chat_id: int, message_id: int, size_type: str = "preview"
    ) -> Optional[str]:
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

            # Mime type check
            mime = ""
            if hasattr(message.media, "document") and hasattr(
                message.media.document, "mime_type"
            ):
                mime = message.media.document.mime_type
                if "webp" in mime:
                    ext = "webp"
                elif "audio/ogg" in mime:
                    ext = "ogg"
                elif "audio/mpeg" in mime:
                    ext = "mp3"
                elif "video/mp4" in mime:
                    ext = "mp4"
                elif "application/x-tgsticker" in mime:
                    ext = "tgs"

            suffix = "_full" if size_type == "full" else ""
            filename = f"media_{chat_id}_{message_id}{suffix}.{ext}"
            path = os.path.join(self.images_dir, filename)

            # Check for existing file (or converted json)
            if ext == "tgs":
                json_filename = filename.replace(".tgs", ".json")
                if os.path.exists(os.path.join(self.images_dir, json_filename)):
                    return f"/cache/{json_filename}"

            if os.path.exists(path):
                return f"/cache/{filename}"

            # Download Logic
            is_special = False
            if isinstance(message.media, MessageMediaDocument):
                for attr in getattr(message.media.document, "attributes", []):
                    if isinstance(
                        attr,
                        (
                            DocumentAttributeSticker,
                            DocumentAttributeAudio,
                            DocumentAttributeVideo,
                        ),
                    ):
                        is_special = True
                        break

            await self.client.download_media(
                message,
                file=path,
                thumb=None if size_type == "full" and not is_special else "m",
            )

            if os.path.exists(path):
                # Auto-convert TGS
                if ext == "tgs":
                    json_path = self._decompress_tgs(path)
                    if json_path:
                        return f"/cache/{os.path.basename(json_path)}"

                return f"/cache/{os.path.basename(path)}"

        except Exception as e:
            logger.error(
                "download_media_failed",
                chat_id=chat_id,
                msg_id=message_id,
                error=str(e),
            )
        return None

    async def get_custom_emoji(self, document_id: int) -> Optional[str]:
        # Check cache for json (tgs converted), webp, or webm
        base_name = f"emoji_{document_id}"
        for ext in ["json", "webp", "webm"]:
            if os.path.exists(os.path.join(self.images_dir, f"{base_name}.{ext}")):
                return f"/cache/{base_name}.{ext}"

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

            final_path = os.path.join(self.images_dir, f"{base_name}.{ext}")
            await self.client.download_media(document, file=final_path)

            if ext == "tgs":
                json_path = self._decompress_tgs(final_path)
                if json_path:
                    return f"/cache/{os.path.basename(json_path)}"

            return f"/cache/{base_name}.{ext}"

        except Exception as e:
            logger.error("download_emoji_failed", doc_id=document_id, error=str(e))
            return None

    def run_maintenance_sync(self, limit_bytes: int):
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

            files.sort(key=lambda x: x[1])

            for path, _, size in files:
                if total_size <= limit_bytes:
                    break
                try:
                    os.remove(path)
                    total_size -= size
                except OSError:
                    pass
        except Exception as e:
            logger.error("cache_maintenance_failed", error=str(e))
