import asyncio
from base64 import b64encode
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from itertools import count

from telethon import functions as api, types as tl, utils

from src.adapters.telegram.sticker_rpc import StickerError

LISTS = {"favorites", "recent"}
FORMATS = {"image/webp": "webp", "application/x-tgsticker": "tgs", "video/webm": "webm"}


def pack_ref(pack):
    return tl.InputStickerSetID(pack.id, pack.access_hash)


def pack_version(pack):
    return pack.hash, pack.count


def pack_snapshot(pack):
    return pack_version(pack), pack.title, pack.short_name, bool(pack.creator)


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


@dataclass(slots=True)
class PackCover:
    sticker_id: int | None
    image_revision: int
    observed: int
    current: bool = True


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
    tile = {
        "id": str(doc.id),
        "source": source,
        "emoji": (attr.alt if attr else "") or "",
        "format": FORMATS.get(doc.mime_type, "unsupported"),
        "thumb": doc.mime_type == "image/webp" or bool(sticker_thumbnail(doc)),
    }
    stripped = next(
        (
            thumb.bytes
            for thumb in doc.thumbs or []
            if isinstance(thumb, tl.PhotoStrippedSize)
            and 3 < len(thumb.bytes) <= 512
            and thumb.bytes[0] == 1
        ),
        None,
    )
    if stripped:
        tile["placeholder"] = b64encode(utils.stripped_photo_to_jpg(stripped)).decode()
    return tile


class StickerCatalog:
    def __init__(self, rpc):
        self._rpc = rpc
        self._packs = {}
        self._covers = {}
        self._collections = OrderedDict()
        self._summaries = None
        self._lists = {}
        self._revisions = count()
        self._loads = {}
        self._load_tasks = set()

    def retire(self):
        for task in self._load_tasks:
            task.cancel()

    async def close(self):
        await asyncio.gather(*self._load_tasks, return_exceptions=True)
        self._lists.clear()
        self._collections.clear()
        self._covers.clear()
        self._packs.clear()
        self._summaries = None

    def _remember_pack(self, pack, revision):
        previous_revision, previous = self._packs.get(pack.id, (-1, None))
        if previous_revision > revision:
            return
        self._packs[pack.id] = revision, pack
        if previous and pack_snapshot(previous) != pack_snapshot(pack):
            source = str(pack.id)
            if pack_version(previous) != pack_version(pack):
                self._collections.pop(source, None)
                if cover := self._covers.get(pack.id):
                    cover.current = False
            # New readers must not join a lookup started before the change.
            self._forget_loads(source, revision)

    def _forget_loads(self, source, revision):
        for key in ((source, False), (source, True)):
            if (load := self._loads.get(key)) and load[0] < revision:
                del self._loads[key]

    async def installed(self):
        return (await self._rpc.read(api.messages.GetAllStickersRequest(0))).sets

    async def _owned(self):
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
        return [pack for pack in result if not (pack.masks or pack.emojis)]

    def _summary(self, pack, installed):
        cover = self._covers.get(pack.id)
        return {
            "id": str(pack.id),
            "name": pack.title,
            "count": pack.count,
            "cover_version": f"{pack.hash}:{pack.count}:{cover.image_revision if cover else 0}",
            "owned": bool(pack.creator),
            "installed": installed,
            "slug": pack.short_name,
        }

    async def summaries(self):
        revision = next(self._revisions)
        installed, owned = await asyncio.gather(self.installed(), self._owned())
        if self._summaries is None or self._summaries[0] < revision:
            packs = {pack.id: pack for pack in installed}
            installed_ids = set(packs)
            packs.update((pack.id, pack) for pack in owned)
            for pack in packs.values():
                self._remember_pack(pack, revision)
            self._summaries = (
                revision,
                {pack_id: pack_id in installed_ids for pack_id in packs},
            )
        return [
            self._summary(self._packs[pack_id][1], installed)
            for pack_id, installed in self._summaries[1].items()
        ]

    def known_pack(self, source):
        if entry := self._packs.get(int(source)):
            return entry[1]

    def is_listed(self, source):
        return self._summaries is not None and int(source) in self._summaries[1]

    def cached_cover_id(self, source):
        # The cover ID can outlive the full sticker list in the LRU cache.
        if (cover := self._covers.get(int(source))) and cover.current:
            return cover.sticker_id

    async def _load_list(self, source):
        revision = next(self._revisions)
        request = (
            api.messages.GetFavedStickersRequest(0)
            if source == "favorites"
            else api.messages.GetRecentStickersRequest(0)
        )
        result = Collection.from_result(await self._rpc.read(request))
        latest = self._lists.get(source)
        # A late reply must not replace a newer read of this list.
        if latest and latest[0] > revision:
            return latest[1]
        self._lists[source] = (revision, result)
        return result

    async def load(self, source):
        if source in LISTS:
            return await self._load_list(source)
        return await self._load_details(source, next(self._revisions))

    async def _load_details(self, source, revision, cached=None, cover=None):
        pack = self.known_pack(source)
        if pack is None or pack.masks or pack.emojis:
            raise StickerError("Pack is not in this workspace. Refresh the pack list.")
        result = await self._rpc.read(
            api.messages.GetStickerSetRequest(
                pack_ref(pack), cached.pack.hash if cached else 0
            )
        )
        if isinstance(result, tl.messages.StickerSetNotModified):
            if cached is None:
                raise StickerError("Telegram did not return the sticker list.")
            if current := self._cached(source):
                return current
            if not (cover and cover.current and self._covers.get(pack.id) is cover):
                return await self._load_shared(source)
            # Eviction alone does not invalidate a successful hash check.
            self._remember_collection(source, cached)
            return cached

        latest, current = self._packs[pack.id]
        if latest > revision and pack_snapshot(current) != pack_snapshot(result.set):
            raise StickerError("Pack changed while loading. Try again.")
        if (cover := self._covers.get(pack.id)) and cover.observed > revision:
            # Prefer the documents from the later lookup, including its file references.
            if newer := self._cached(source):
                return newer
            raise StickerError("Pack changed while loading. Try again.")
        collection = Collection.from_result(result, is_pack=True)
        self.remember(source, collection, revision=revision)
        return collection

    async def load_pack(self, ref):
        return Collection.from_result(
            await self._rpc.read(api.messages.GetStickerSetRequest(ref, 0)),
            is_pack=True,
        )

    def remember(self, source, result, *, revision=None):
        if revision is None:
            revision = next(self._revisions)
        # A fresh read or a write result supersedes an older shared lookup.
        self._forget_loads(source, revision)
        self._remember_pack(result.pack, revision)
        sticker_id = next(iter(result.docs), None)
        previous = self._covers.get(result.pack.id)
        version = (
            previous.image_revision + (previous.sticker_id != sticker_id)
            if previous
            else 0
        )
        self._covers[result.pack.id] = PackCover(sticker_id, version, revision)
        self._remember_collection(source, result)

    def _remember_collection(self, source, collection):
        self._collections[source] = collection
        self._collections.move_to_end(source)
        if len(self._collections) > 32:
            self._collections.popitem(last=False)

    async def items(self, source):
        if source in LISTS:
            collection = await self._load_list(source)
        else:
            cached = self._collections.get(source)
            if cached and cached.pack.hash and (source, False) not in self._loads:
                collection = await self._load_shared(source, cached)
            else:
                collection = await self._load_shared(source)
        return [sticker_tile(source, doc) for doc in collection.docs.values()]

    def _cached(self, source):
        if snapshot := self._lists.get(source):
            return snapshot[1]
        if collection := self._collections.get(source):
            self._collections.move_to_end(source)
            return collection

    async def for_artwork(self, source):
        if collection := self._cached(source):
            return collection
        if source not in LISTS and self.known_pack(source) is None:
            return None
        return await self._load_shared(source)

    async def refresh_document(self, source, doc):
        if collection := self._cached(source):
            current = collection.docs.get(doc.id)
            if current and current.file_reference != doc.file_reference:
                return current
        return (await self._load_shared(source)).docs.get(doc.id)

    async def _load_shared(self, source, cached=None):
        key = (source, cached is not None)
        load = self._loads.get(key)
        if load is None:

            def forget(done):
                self._load_tasks.discard(done)
                if self._loads.get(key) == (revision, done):
                    self._loads.pop(key)
                if not done.cancelled():
                    done.exception()  # All requesting clients may have disconnected.

            revision = next(self._revisions)
            task = asyncio.create_task(
                self._load_list(source)
                if source in LISTS
                else self._load_details(
                    source,
                    revision,
                    cached,
                    self._covers.get(int(source)) if cached else None,
                )
            )
            load = self._loads[key] = (revision, task)
            self._load_tasks.add(task)
            task.add_done_callback(forget)
        return await asyncio.shield(load[1])
