import asyncio
import hashlib

from telethon import errors

from src.adapters.telegram.media import MediaBusy, MediaTooLarge, _download_bytes
from src.adapters.telegram.sticker_catalog import sticker_thumbnail
from src.adapters.telegram.sticker_rpc import (
    FILE_REFERENCE_ERRORS,
    StickerError,
    rpc_error,
)
from src.domain.models import MediaContent

MAX_STICKER_BYTES = 1024 * 1024


class StickerMedia:
    def __init__(self, client, media, catalog, check):
        self.client = client
        self.media = media
        self.catalog = catalog
        self.check = check
        self.downloads = asyncio.Semaphore(4)

    async def transfer(self, source, doc, mode):
        async def download():
            nonlocal doc
            for attempt in range(2):
                self.check()
                thumb = None
                if mode == "thumb":
                    # An absent thumbnail must never trigger a full download.
                    thumb = sticker_thumbnail(doc)
                    if thumb is None or getattr(thumb, "size", 0) > MAX_STICKER_BYTES:
                        return None
                elif not 0 < doc.size <= MAX_STICKER_BYTES:
                    raise StickerError("Sticker exceeds the 1 MiB transfer limit.")
                try:
                    async with self.downloads, asyncio.timeout(20):
                        data = await _download_bytes(
                            self.client.download_media,
                            doc,
                            thumb=thumb,
                            limit=MAX_STICKER_BYTES,
                        )
                    if not data or len(data) > MAX_STICKER_BYTES:
                        return None
                    mime = doc.mime_type
                    if mode == "thumb":
                        mime = (
                            "image/webp"
                            if data.startswith(b"RIFF")
                            else "image/png"
                            if data.startswith(b"\x89PNG")
                            else "image/jpeg"
                        )
                    return MediaContent(data=data, mime_type=mime)
                except FILE_REFERENCE_ERRORS as error:
                    if attempt:
                        raise rpc_error(error) from error
                    doc = (await self.catalog.load(source)).docs.get(doc.id)
                    if doc is None:
                        raise StickerError(
                            "Sticker is no longer in this list."
                        ) from error
                except (errors.FloodWaitError, errors.FloodPremiumWaitError) as error:
                    raise StickerError(
                        f"Telegram requires a wait of {error.seconds}s before downloading this sticker."
                    ) from error
                except errors.RPCError as error:
                    raise rpc_error(error) from error
            return None

        return await self.media._shared(("sticker", doc.id, mode), download)

    async def artwork(self, source, doc_id, mode):
        self.check()
        collection = await self.catalog.for_artwork(source)
        if collection is None:
            return None
        self.check()
        doc = collection.docs.get(doc_id)
        if not doc or (
            mode == "full" and doc.mime_type not in {"image/webp", "video/webm"}
        ):
            return None
        return await self.transfer(source, doc, mode)

    async def original(self, source, doc):
        try:
            content = await self.transfer(source, doc, "full")
        except (MediaBusy, MediaTooLarge, OSError) as error:
            raise StickerError(
                "Original sticker media is unavailable. Refresh and try again."
            ) from error
        except errors.RPCError as error:
            raise rpc_error(error) from error
        if not content or len(content.data) != doc.size:
            raise StickerError("Could not retrieve the original sticker bytes.")
        return content.data

    async def digest(self, source, doc, hashes, *, data=None):
        if doc.id not in hashes:
            raw = data if data is not None else await self.original(source, doc)
            hashes[doc.id] = await asyncio.to_thread(
                lambda: hashlib.sha256(raw).digest()
            )
        return hashes[doc.id]
