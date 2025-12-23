import os
from typing import Optional, Any
from telethon import utils
from telethon.tl.types import MessageMediaDocument, DocumentAttributeSticker, DocumentAttributeAudio, DocumentAttributeVideo
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

class MediaMixin:
    def __init__(self):
        self.client: Any = None
        self.images_dir: str = ""

    async def _get_chat_image(self, entity: Any, chat_id: int) -> Optional[str]:
        filename = f"{chat_id}.jpg"
        path = os.path.join(self.images_dir, filename)
        if os.path.exists(path): return f"/cache/{filename}"
        try:
            result = await self.client.download_profile_photo(entity, file=path, download_big=False)
            if result: return f"/cache/{filename}"
        except Exception: pass
        return None

    async def download_media(self, chat_id: int, message_id: int) -> Optional[str]:
        try:
            entity = await self.client.get_entity(chat_id)
            messages = await self.client.get_messages(entity, ids=[message_id])
            if not messages:
                return None
            message = messages[0]

            if not message or not getattr(message, 'media', None):
                return None

            ext = "jpg"
            guessed_ext = utils.get_extension(message.media)
            if guessed_ext:
                ext = guessed_ext.lstrip('.')

            if hasattr(message.media, 'document'):
                 if hasattr(message.media.document, 'mime_type'):
                     if 'webp' in message.media.document.mime_type: ext = "webp"
                     elif 'audio/ogg' in message.media.document.mime_type: ext = "ogg"
                     elif 'audio/mpeg' in message.media.document.mime_type: ext = "mp3"
                     elif 'video/mp4' in message.media.document.mime_type: ext = "mp4"

            filename = f"media_{chat_id}_{message_id}.{ext}"
            path = os.path.join(self.images_dir, filename)

            if os.path.exists(path):
                return f"/cache/{filename}"

            is_sticker = False
            is_audio = False
            is_video = False

            if isinstance(message.media, MessageMediaDocument):
                 for attr in getattr(message.media.document, 'attributes', []):
                      if isinstance(attr, DocumentAttributeSticker): is_sticker = True
                      if isinstance(attr, DocumentAttributeAudio): is_audio = True
                      if isinstance(attr, DocumentAttributeVideo): is_video = True

            result = None
            if is_sticker or is_audio or is_video:
                 result = await self.client.download_media(message, file=path)
            else:
                 result = await self.client.download_media(message, file=path, thumb='m')
                 if not result:
                     result = await self.client.download_media(message, file=path)

            if result:
                 final_filename = os.path.basename(result)
                 return f"/cache/{final_filename}"

        except Exception as e:
            logger.error("download_media_failed", chat_id=chat_id, message_id=message_id, error=str(e))
        return None
