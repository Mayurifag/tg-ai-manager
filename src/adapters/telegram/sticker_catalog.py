import asyncio
from collections import OrderedDict, defaultdict
from dataclasses import dataclass

from telethon import functions as api, types as tl

from src.adapters.telegram.sticker_rpc import StickerError

LISTS = {"favorites", "recent"}
FORMATS = {"image/webp": "webp", "application/x-tgsticker": "tgs", "video/webm": "webm"}


def pack_ref(pack):
    return tl.InputStickerSetID(pack.id, pack.access_hash)


def sticker_attr(doc):
    return next(
        (a for a in doc.attributes if isinstance(a, tl.DocumentAttributeSticker)), None
    )


def belongs(doc, pack):
    attr = sticker_attr(doc)
    ref = attr and attr.stickerset
    return (isinstance(ref, tl.InputStickerSetID) and ref.id == pack.id) or (
        isinstance(ref, tl.InputStickerSetShortName)
        and ref.short_name.lower() == pack.short_name.lower()
    )


@dataclass
class Collection:
    docs: dict
    emoji: dict
    pack: object = None

    @classmethod
    def from_result(cls, result, *, is_pack=False):
        emoji = defaultdict(list)
        for group in result.packs:
            for doc_id in group.documents:
                emoji[doc_id].append(group.emoticon)
        docs = result.documents if is_pack else result.stickers
        return cls(
            {d.id: d for d in docs if isinstance(d, tl.Document)},
            emoji,
            result.set if is_pack else None,
        )


def require_owned(collection):
    if not collection.pack or not collection.pack.creator:
        raise StickerError("Only packs you own can be edited.")


def sticker_thumbnail(doc):
    return max(
        (
            thumb
            for thumb in doc.thumbs or []
            if isinstance(
                thumb, (tl.PhotoSize, tl.PhotoCachedSize, tl.PhotoStrippedSize)
            )
        ),
        key=lambda thumb: getattr(thumb, "w", 0) * getattr(thumb, "h", 0),
        default=None,
    )


def sticker_tile(source, doc):
    attr = sticker_attr(doc)
    return {
        "id": str(doc.id),
        "source": source,
        "emoji": (attr.alt if attr else "") or "",
        "format": FORMATS.get(doc.mime_type, "unsupported"),
        "thumb": bool(sticker_thumbnail(doc)),
    }


class StickerCatalog:
    def __init__(self, rpc):
        self._rpc = rpc
        self._packs = {}
        self._collections = OrderedDict()
        self._artwork_loads = {}

    def retire(self):
        for task in self._artwork_loads.values():
            task.cancel()

    async def close(self):
        await asyncio.gather(*self._artwork_loads.values(), return_exceptions=True)
        self._collections.clear()
        self._packs.clear()

    async def installed(self):
        sets = (await self._rpc.read(api.messages.GetAllStickersRequest(0))).sets
        self._packs.update((pack.id, pack) for pack in sets)
        return sets

    async def owned(self):
        offset, result = 0, []
        while True:
            page = await self._rpc.read(api.messages.GetMyStickersRequest(offset, 100))
            sets = [item.set for item in page.sets]
            result.extend(sets)
            if not sets or len(result) >= page.count:
                break
            if offset == sets[-1].id:
                raise StickerError(
                    "Telegram repeated a page of owned packs. Refresh and try again."
                )
            offset = sets[-1].id
        self._packs.update((pack.id, pack) for pack in result)
        return [pack for pack in result if not (pack.masks or pack.emojis)]

    @staticmethod
    def summary(pack, installed):
        return {
            "id": str(pack.id),
            "name": pack.title,
            "count": pack.count,
            "owned": bool(pack.creator),
            "installed": installed,
            "slug": pack.short_name,
        }

    async def summaries(self):
        installed, owned = await asyncio.gather(self.installed(), self.owned())
        installed_ids = {p.id for p in installed}
        owned_by_id = {p.id: p for p in owned}
        return [self.summary(owned_by_id.get(p.id, p), True) for p in installed] + [
            self.summary(p, False) for p in owned if p.id not in installed_ids
        ]

    def known_pack(self, source):
        return self._packs.get(int(source))

    async def load(self, source):
        if source == "favorites":
            result = Collection.from_result(
                await self._rpc.read(api.messages.GetFavedStickersRequest(0))
            )
        elif source == "recent":
            result = Collection.from_result(
                await self._rpc.read(api.messages.GetRecentStickersRequest(0))
            )
        else:
            pack = self.known_pack(source)
            if pack is None or pack.masks or pack.emojis:
                raise StickerError(
                    "Pack is not in this workspace. Refresh the pack list."
                )
            result = await self.load_pack(pack_ref(pack))
        self.remember(source, result)
        return result

    async def load_pack(self, ref):
        result = Collection.from_result(
            await self._rpc.read(api.messages.GetStickerSetRequest(ref, 0)),
            is_pack=True,
        )
        self._packs[result.pack.id] = result.pack
        return result

    def remember(self, source, result):
        if result.pack is not None:
            self._packs[result.pack.id] = result.pack
        self._collections[source] = result
        self._collections.move_to_end(source)
        if len(self._collections) > 32:
            self._collections.popitem(last=False)

    async def items(self, source):
        collection = await self.load(source)
        return [sticker_tile(source, doc) for doc in collection.docs.values()]

    async def for_artwork(self, source):
        if source in self._collections:
            self._collections.move_to_end(source)
            return self._collections[source]
        if source not in LISTS and self.known_pack(source) is None:
            return None
        return await self._load_for_artwork(source)

    async def _load_for_artwork(self, source):
        task = self._artwork_loads.get(source)
        if task is None:

            def forget(done):
                self._artwork_loads.pop(source)
                if not done.cancelled():
                    done.exception()  # All requesting clients may have disconnected.

            task = self._artwork_loads[source] = asyncio.create_task(self.load(source))
            task.add_done_callback(forget)
        return await asyncio.shield(task)
