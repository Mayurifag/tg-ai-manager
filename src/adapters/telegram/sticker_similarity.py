import asyncio
from collections import defaultdict
from io import BytesIO
from math import cos, pi
from statistics import median

from PIL import Image, ImageOps, UnidentifiedImageError
from telethon import errors

from src.adapters.telegram.media import MediaBusy, MediaTooLarge
from src.adapters.telegram.sticker_catalog import sticker_tile
from src.adapters.telegram.sticker_rpc import StickerError

SCAN_LIMIT = 250
HASH_SIDE = 8
SAMPLE_SIDE = 24
BASIS = [
    [cos(pi * (x + 0.5) * frequency / SAMPLE_SIDE) for x in range(SAMPLE_SIDE)]
    for frequency in range(HASH_SIDE)
]


def thumbnail_hash(data: bytes) -> int | None:
    try:
        with Image.open(BytesIO(data), formats=("JPEG", "WEBP", "PNG")) as image:
            if max(image.size) > 1024 or getattr(image, "is_animated", False):
                return None
            art = image.convert("RGBA")
            # Crop transparent margins. Composite onto a fixed gray background
            # so hidden RGB values and the site's theme don't affect the hash.
            bounds = art.getchannel("A").getbbox()
            if not bounds:
                return None
            art = ImageOps.contain(
                art.crop(bounds), (SAMPLE_SIDE, SAMPLE_SIDE), Image.Resampling.LANCZOS
            )
            canvas = Image.new("RGBA", (SAMPLE_SIDE, SAMPLE_SIDE), (127, 127, 127, 255))
            canvas.alpha_composite(
                art, ((SAMPLE_SIDE - art.width) // 2, (SAMPLE_SIDE - art.height) // 2)
            )
            pixels = canvas.convert("L").get_flattened_data()
    except UnidentifiedImageError, OSError, Image.DecompressionBombError:
        return None
    # Omit the DC coefficient so uniform brightness changes do not dominate.
    rows = [pixels[i : i + SAMPLE_SIDE] for i in range(0, len(pixels), SAMPLE_SIDE)]
    columns = [
        [sum(a * b for a, b in zip(row, basis)) for row in rows] for basis in BASIS
    ]
    coefficients = [
        sum(a * b for a, b in zip(column, basis))
        for column in columns
        for basis in BASIS
    ][1:]
    threshold = median(coefficients)
    return sum(1 << i for i, value in enumerate(coefficients) if value > threshold)


def rank_matches(ids, verified, visual):
    groups = defaultdict(list)
    for i, doc_id in enumerate(ids):
        groups[verified.get(doc_id, doc_id)].append(i)
    exact = [group for group in groups.values() if len(group) > 1]
    pairs = []
    for i, left in enumerate(visual):
        if left is None:
            continue
        for j in range(i + 1, len(visual)):
            right = visual[j]
            if right is None or verified.get(ids[i], ids[i]) == verified.get(
                ids[j], ids[j]
            ):
                continue
            distance = (left ^ right).bit_count()
            if distance <= 10:
                pairs.append((distance, i, j))
    return exact, sorted(pairs)[:12]


async def scan_duplicates(catalog, media, op, sources):
    def check_canceled():
        if op.cancel_requested:
            raise asyncio.CancelledError

    count = 0
    for source in sources:
        pack = catalog.known_pack(source)
        if not pack:
            raise StickerError("Refresh the pack list before scanning.")
        count += pack.count
    limit_error = f"Select fewer packs. A scan can check at most {SCAN_LIMIT} stickers."
    if count > SCAN_LIMIT:
        raise StickerError(limit_error)
    entries = []
    for source in sources:
        check_canceled()
        collection = await catalog.load(source)
        entries.extend((source, collection, doc) for doc in collection.docs.values())
        if len(entries) > SCAN_LIMIT:
            raise StickerError(limit_error)
    sizes = defaultdict(set)
    for _, _, doc in entries:
        sizes[doc.size].add(doc.id)
    hashes, verified, representatives, visual = {}, {}, {}, []
    missing = 0
    for i, (source, _, doc) in enumerate(entries, 1):
        check_canceled()
        op.message = f"Checking sticker {i} of {len(entries)}…"
        if len(sizes[doc.size]) > 1:
            try:
                fingerprint = await media.digest(source, doc, hashes)
                verified[doc.id] = representatives.setdefault(fingerprint, doc.id)
            except StickerError:
                missing += 1
        value = None
        if doc.mime_type == "image/webp":
            try:
                thumb = await media.transfer(source, doc, "thumb")
                if thumb:
                    value = await asyncio.to_thread(thumbnail_hash, thumb.data)
            except (
                StickerError,
                MediaBusy,
                MediaTooLarge,
                OSError,
                errors.RPCError,
            ):
                pass
            if value is None:
                missing += 1
        visual.append(value)
    check_canceled()
    exact, pairs = await asyncio.to_thread(
        rank_matches, [d.id for _, _, d in entries], verified, visual
    )

    check_canceled()

    def tile(i):
        source, collection, doc = entries[i]
        return sticker_tile(source, doc) | {
            "pack": collection.pack.title,
            "owned": bool(collection.pack.creator),
        }

    op.result.update(
        exact=[[tile(i) for i in group] for group in exact],
        lookalikes=[
            {"distance": score, "items": [tile(i), tile(j)]} for score, i, j in pairs
        ],
    )
    op.message = f"Checked {len(entries)} stickers." + (
        f" {missing} byte or thumbnail checks were unavailable." if missing else ""
    )
