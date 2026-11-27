import asyncio
import hashlib
from io import BytesIO
from itertools import islice

from PIL import Image
from telethon import errors

from src.adapters.telegram.media import MediaBusy, MediaTooLarge, _download_bytes
from src.adapters.telegram.sticker_catalog import sticker_thumbnail
from src.adapters.telegram.sticker_rpc import (
    FILE_REFERENCE_ERRORS,
    StickerError,
    rpc_error,
)
from src.domain.models import MediaContent
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)
MAX_STICKER_BYTES = 1024 * 1024
DISPLAY_EDGE = 192
GRID_DOWNLOADS = 6
PREFETCH_PER_LIST = 24


def display_key(doc_id):
    return "sticker", doc_id, "display"


def display_webp(data):
    try:
        with (
            BytesIO(data) as source,
            Image.open(source, formats=("WEBP", "PNG", "JPEG")) as image,
        ):
            edge = max(image.size)
            if edge > 2048:
                return None
            if (
                image.format == "WEBP"
                and edge <= DISPLAY_EDGE
                and len(data) <= 32 * 1024
                and not image.is_animated
            ):
                image.load()
            else:
                with image.convert("RGBA") as artwork, BytesIO() as output:
                    artwork.thumbnail(
                        (DISPLAY_EDGE, DISPLAY_EDGE), Image.Resampling.LANCZOS
                    )
                    artwork.save(output, "WEBP", quality=82, method=2)
                    data = output.getvalue()
            return MediaContent(data=data, mime_type="image/webp")
    except Image.DecompressionBombError, OSError, ValueError:
        return None


class StickerMedia:
    def __init__(self, client, media, catalog, check):
        self.client = client
        self.media = media
        self.catalog = catalog
        self.check = check
        self.downloads = asyncio.Semaphore(8)
        self.grid_downloads = asyncio.Semaphore(GRID_DOWNLOADS)

    async def transfer(self, source, doc, mode):
        async def download():
            self.check()
            expected_size = doc.size
            thumb = None
            if mode == "thumb":
                thumb = sticker_thumbnail(doc)
                expected_size = getattr(thumb, "size", 0)
                if thumb is None or expected_size > MAX_STICKER_BYTES:
                    return None
            elif not 0 < expected_size <= MAX_STICKER_BYTES:
                raise StickerError("Sticker exceeds the 1 MiB transfer limit.")
            try:
                async with self.downloads, asyncio.timeout(20):
                    data = await _download_bytes(
                        self.client.download_media,
                        doc,
                        thumb=thumb,
                        limit=MAX_STICKER_BYTES,
                    )
            except FILE_REFERENCE_ERRORS:
                raise
            except (errors.FloodWaitError, errors.FloodPremiumWaitError) as error:
                raise StickerError(
                    f"Telegram requires a wait of {error.seconds}s before downloading this sticker."
                ) from error
            except errors.LocationInvalidError:
                # The thumbnail and original use different file locations.
                return None
            except errors.RPCError as error:
                raise rpc_error(error) from error
            if not data or (expected_size and len(data) != expected_size):
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

        key = ("sticker", doc.id, mode)

        def shared():
            # Successful bytes are immutable, but an expired location applies
            # only to callers that used the same Telegram file reference.
            return self.media._shared(
                (*key, doc.file_reference), download, immutable_cache_key=key
            )

        try:
            return await shared()
        except FILE_REFERENCE_ERRORS as error:
            # A shared download may have callers from different lists.
            doc = await self.catalog.refresh_document(source, doc)
            if doc is None:
                raise StickerError("Sticker is no longer in this list.") from error
        try:
            return await shared()
        except FILE_REFERENCE_ERRORS as error:
            raise rpc_error(error) from error

    async def display(self, source, doc):
        key = display_key(doc.id)
        if cached := self.media._get(key):
            return cached
        thumb = sticker_thumbnail(doc)
        modes = ("thumb",)
        if doc.mime_type == "image/webp" and 0 < doc.size <= MAX_STICKER_BYTES:
            modes = (
                ("full", "thumb")
                if max(getattr(thumb, "w", 0), getattr(thumb, "h", 0)) < 96
                else ("thumb", "full")
            )
        for mode in modes:
            if mode == "thumb" and thumb is None:
                continue
            # Get the shared bytes before taking a slot for the conversion.
            if content := await self.transfer(source, doc, mode):
                if image := await self.media._shared(
                    key, lambda: asyncio.to_thread(display_webp, content.data)
                ):
                    return image
        return None

    async def artwork(self, source, doc_id, mode):
        self.check()
        if doc_id is None:
            if not self.catalog.is_listed(source):
                return None
            cover_id = self.catalog.cached_cover_id(source)
            if cover_id is not None and (
                cached := self.media._get(display_key(cover_id))
            ):
                return cached
        collection = await self.catalog.for_artwork(source)
        if collection is None:
            return None
        self.check()
        doc = (
            next(iter(collection.docs.values()), None)
            if doc_id is None
            else collection.docs.get(doc_id)
        )
        if not doc or (
            mode == "full" and doc.mime_type not in {"image/webp", "video/webm"}
        ):
            return None
        if mode == "display":
            return await self.display(source, doc)
        return await self.transfer(source, doc, mode)

    async def grid_artwork(self, entries):
        async def load(source, doc_id):
            async with self.grid_downloads:
                try:
                    image = await self.artwork(source, doc_id, "display")
                except StickerError, MediaBusy, MediaTooLarge, OSError:
                    image = None
                return source, doc_id, image

        remaining = iter(entries)
        pending, done = set(), set()

        def start_next():
            if entry := next(remaining, None):
                pending.add(asyncio.create_task(load(*entry)))

        try:
            for _ in range(GRID_DOWNLOADS):
                start_next()
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                while done:
                    self.check()
                    yield done.pop().result()
                    start_next()
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, *done, return_exceptions=True)

    async def prefetch(self):
        for source in ("recent", "favorites"):
            try:
                collection = await self.catalog.for_artwork(source)
                for doc in islice(collection.docs.values(), PREFETCH_PER_LIST):
                    self.check()
                    async with self.grid_downloads:
                        await self.display(source, doc)
            except (StickerError, MediaBusy, MediaTooLarge, OSError) as error:
                logger.info(
                    "sticker_prefetch_skipped",
                    source=source,
                    category=type(error).__name__,
                )

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
