from dataclasses import dataclass

from telethon import functions as api, types as tl, utils

from src.adapters.telegram.sticker_catalog import (
    FORMATS,
    Collection,
    belongs,
    pack_ref,
    require_owned,
    sticker_attr,
)
from src.adapters.telegram.sticker_rpc import (
    FILE_REFERENCE_ERRORS,
    StickerError,
    Uncertain,
    rpc_error,
)


@dataclass(frozen=True)
class Replacement:
    destination: str
    destination_ids: frozenset[int]
    origin: str | None = None
    origin_ids: frozenset[int] = frozenset()


class StickerCopy:
    def __init__(self, catalog, media, rpc, edits):
        self.catalog = catalog
        self.media = media
        self.rpc = rpc
        self.edits = edits

    async def copy(self, op, entries, destination, title, slug, install, covered=None):
        covered = dict(covered or {})
        sources = {}
        for source, _ in entries:
            if source not in sources:
                sources[source] = await self.catalog.load(source)
        target = await self.catalog.load(destination) if destination else None
        if target:
            require_owned(target)
        hashes, failed = {}, []
        copied, duplicates = 0, 0
        for i, (source, doc_id) in enumerate(entries):
            if (source, doc_id) in covered:
                continue
            op.message = f"Copying sticker {i + 1} of {len(entries)}…"
            doc = sources[source].docs.get(doc_id)
            attr = doc and sticker_attr(doc)
            if not attr or attr.mask or doc.mime_type not in FORMATS:
                failed.append(
                    {
                        "id": str(doc_id),
                        "reason": "Sticker is missing or uses an unsupported format.",
                    }
                )
                continue
            try:
                candidates = (
                    [d for d in target.docs.values() if belongs(d, target.pack)]
                    if target
                    else []
                )
                match = next((d for d in candidates if d.id == doc_id), None)
                data = None
                if match is None:
                    data = await self.media.original(source, doc)
                    for candidate in candidates:
                        if candidate.size != doc.size:
                            continue
                        if await self.media.digest(
                            source, doc, hashes, data=data
                        ) == await self.media.digest(
                            str(target.pack.id), candidate, hashes
                        ):
                            match = candidate
                            break
                if match:
                    covered[source, doc_id] = match.id
                    duplicates += 1
                    continue

                uploaded = await self._upload_sticker(op, doc, attr, data)
                emoji = (
                    "".join(dict.fromkeys(sources[source].emoji[doc_id]))
                    or attr.alt
                    or "🙂"
                )
                item = tl.InputStickerSetItem(utils.get_input_document(uploaded), emoji)
                before = set(target.docs) if target else set()
                target = await self._write_destination(
                    op, item, target, before, title=title, slug=slug
                )
                destination = str(target.pack.id)
                self.catalog.remember(destination, target)
                op.result.update(destination=destination, slug=target.pack.short_name)
                new_docs = [
                    d
                    for d in target.docs.values()
                    if d.id not in before and d.id != doc_id and belongs(d, target.pack)
                ]
                match = next((d for d in new_docs if d.id == uploaded.id), None)
                if not match:
                    for candidate in new_docs:
                        if candidate.size != doc.size:
                            continue
                        try:
                            if await self.media.digest(
                                source, doc, hashes, data=data
                            ) == await self.media.digest(
                                destination, candidate, hashes
                            ):
                                match = candidate
                                break
                        except StickerError:
                            # This write already ran. A failed verification must
                            # not make it eligible for a retry.
                            continue
                if not match:
                    raise Uncertain(
                        "Telegram returned a pack, but the independent copy could not be verified. The original was not uninstalled."
                    )
                covered[source, doc_id] = match.id
                copied += 1
            except (StickerError, *FILE_REFERENCE_ERRORS) as error:
                reason = str(
                    rpc_error(error)
                    if isinstance(error, FILE_REFERENCE_ERRORS)
                    else error
                )
                failed.append({"id": str(doc_id), "reason": reason})
                op.result.update(
                    copied=copied,
                    duplicates=duplicates,
                    failures=failed,
                    remaining=len(entries) - i - 1,
                )
                if isinstance(error, Uncertain):
                    raise
                op.retry = dict(
                    entries=entries,
                    destination=destination,
                    title=title,
                    slug=slug,
                    install=install,
                    covered=covered,
                )
                raise StickerError(
                    "Copy stopped. The original was not uninstalled. " + reason
                ) from error

        op.result.update(copied=copied, duplicates=duplicates, failures=failed)
        if not target:
            raise StickerError("No supported stickers were copied.")
        destination = str(target.pack.id)
        op.result.update(destination=destination, slug=target.pack.short_name)
        try:
            fresh = await self.catalog.load(destination)
        except StickerError as error:
            raise Uncertain(
                "Could not recheck the destination after copying. The original was not uninstalled."
            ) from error
        verified = {d.id for d in fresh.docs.values() if belongs(d, fresh.pack)}
        if not set(covered.values()) <= verified:
            raise Uncertain(
                "The destination changed and not all stickers can be verified. The original was not uninstalled."
            )
        if failed:
            raise StickerError(
                "Some stickers could not be copied. The original was not uninstalled."
            )
        replacement = Replacement(destination, frozenset(covered.values()))
        if len(sources) == 1:
            origin, original = next(iter(sources.items()))
            if original.pack and origin != destination:
                origin_ids = frozenset(
                    doc_id for source, doc_id in covered if source == origin
                )
                if origin_ids and self._has_pack_contents(original, origin_ids):
                    replacement = Replacement(
                        destination, replacement.destination_ids, origin, origin_ids
                    )
        if install:
            await self._install_copy(op, replacement, fresh.pack)
        else:
            install = any(p.id == fresh.pack.id for p in await self.catalog.installed())
        await self._complete_copy(op, replacement, install)
        op.message = (
            f"Copied {copied}. {duplicates} exact duplicates already covered."
            + (
                " Destination is installed."
                if install
                else " Destination is not installed."
            )
        )

    async def _upload_sticker(self, op, doc, attr, data):
        filename = f"sticker.{FORMATS[doc.mime_type]}"
        attributes = [
            a
            for a in doc.attributes
            if isinstance(
                a,
                (
                    tl.DocumentAttributeImageSize,
                    tl.DocumentAttributeVideo,
                    tl.DocumentAttributeAnimated,
                ),
            )
        ]
        attributes += [
            tl.DocumentAttributeFilename(filename),
            tl.DocumentAttributeSticker(attr.alt, tl.InputStickerSetEmpty()),
        ]
        upload = await self.rpc.upload_bytes(op, data, filename)
        result = await self.rpc.write(
            op,
            api.messages.UploadMediaRequest(
                tl.InputPeerSelf(),
                tl.InputMediaUploadedDocument(
                    upload, doc.mime_type, attributes, force_file=True
                ),
            ),
        )
        document = getattr(result, "document", None)
        if not isinstance(document, tl.Document):
            raise StickerError("Telegram did not return an uploaded sticker document.")
        return document

    async def _write_destination(self, op, item, target, before, *, title, slug):
        ref = pack_ref(target.pack) if target else tl.InputStickerSetShortName(slug)

        async def verify_outcome():
            result = await self.catalog.load_pack(ref)
            # A new attributable document might have a different ID. The caller
            # verifies its bytes after leaving the shared write worker.
            if result.pack.creator and any(
                d.id not in before and belongs(d, result.pack)
                for d in result.docs.values()
            ):
                return result
            return None

        async def guard_target():
            current = await self.catalog.load_pack(ref)
            require_owned(current)
            if list(current.docs) != list(target.docs):
                raise StickerError(
                    "The destination changed in Telegram. Refresh before retrying."
                )

        request = (
            api.stickers.AddStickerToSetRequest(ref, item)
            if target
            else api.stickers.CreateStickerSetRequest(
                tl.InputUserSelf(), title, slug, [item]
            )
        )
        result = await self.rpc.write(
            op,
            request,
            reconcile=verify_outcome,
            guard=guard_target if target else None,
        )
        if isinstance(result, tl.messages.StickerSet):
            result = Collection.from_result(result, is_pack=True)
        if not isinstance(result, Collection):
            raise Uncertain(
                "Telegram did not return the destination pack after the write. The original was not uninstalled."
            )
        if not result.pack.creator:
            raise Uncertain(
                "Could not confirm ownership of the destination. The original was not uninstalled."
            )
        return result

    async def _install_copy(self, op, replacement, pack):
        try:
            await self.edits.ensure_installed(op, pack)
        except Uncertain:
            raise
        except StickerError as error:
            await self._complete_copy(op, replacement, installed=False)
            raise StickerError(
                "All stickers are covered, but installation failed. The original was not uninstalled. "
                + str(error)
            ) from error

    async def _complete_copy(self, op, replacement, installed):
        destination, origin = replacement.destination, replacement.origin
        op.replacement = replacement
        op.result.update(
            destination=destination,
            slug=self.catalog.known_pack(destination).short_name,
            can_install=not installed,
        )
        if (
            installed
            and origin
            and any(str(p.id) == origin for p in await self.catalog.installed())
        ):
            try:
                original = await self.catalog.load(origin)
            except StickerError:
                return
            if self._has_pack_contents(original, replacement.origin_ids):
                op.result["uninstall_original"] = {
                    "name": original.pack.title,
                    "count": original.pack.count,
                }

    async def _verified_replacement(self, replacement):
        target = await self.catalog.load(replacement.destination)
        require_owned(target)
        if not replacement.destination_ids <= {
            d.id for d in target.docs.values() if belongs(d, target.pack)
        }:
            raise StickerError(
                "The destination changed. The original was not uninstalled."
            )
        return target

    @staticmethod
    def _has_pack_contents(collection, ids):
        return collection.pack.count == len(ids) and collection.docs.keys() == ids

    async def finish(self, op, replacement, uninstall):
        if uninstall:
            origin = replacement.origin
            if not origin:
                raise StickerError("No original pack is eligible for uninstall.")

            async def guard():
                target = await self._verified_replacement(replacement)
                if not any(
                    p.id == target.pack.id for p in await self.catalog.installed()
                ):
                    raise StickerError(
                        "The destination is not installed. The original was not uninstalled."
                    )
                if not self._has_pack_contents(
                    await self.catalog.load(origin), replacement.origin_ids
                ):
                    raise StickerError(
                        "The original pack changed after the copy. It was not uninstalled."
                    )

            await self.edits.uninstall(op, [origin], guard=guard)
        else:
            target = await self._verified_replacement(replacement)
            await self._install_copy(op, replacement, target.pack)
            await self._complete_copy(op, replacement, True)
            op.message = "Destination installed. The original was not uninstalled."
