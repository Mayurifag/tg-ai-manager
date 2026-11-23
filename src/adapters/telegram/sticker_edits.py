from collections import defaultdict

from telethon import functions as api, types as tl, utils

from src.adapters.telegram.sticker_catalog import belongs, pack_ref, require_owned
from src.adapters.telegram.sticker_rpc import (
    FILE_REFERENCE_ERRORS,
    StickerError,
    Uncertain,
    rpc_error,
)


class StickerEdits:
    def __init__(self, catalog, rpc):
        self.catalog = catalog
        self.rpc = rpc

    async def ensure_installed(self, op, pack):
        async def installed():
            return any(p.id == pack.id for p in await self.catalog.installed())

        if not await installed():
            result = await self.rpc.write(
                op,
                api.messages.InstallStickerSetRequest(pack_ref(pack), False),
                reconcile=installed,
            )
            if isinstance(result, tl.messages.StickerSetInstallResultArchive):
                op.result["archived"] = [item.set.title for item in result.sets]
        try:
            if await installed():
                return
        except StickerError:
            pass
        raise Uncertain(
            "Could not confirm that the destination is installed. The original was not uninstalled."
        )

    async def uninstall(self, op, sources, guard=None):
        installed = {str(p.id): p for p in await self.catalog.installed()}
        packs = [installed[s] for s in sources if s in installed]
        if packs:
            ids = {p.id for p in packs}

            async def verify():
                return not ids.intersection(
                    p.id for p in await self.catalog.installed()
                )

            await self.rpc.write(
                op,
                api.messages.ToggleStickerSetsRequest(
                    [pack_ref(p) for p in packs], uninstall=True
                ),
                reconcile=verify,
                guard=guard,
            )
            if not await verify():
                raise Uncertain(
                    "Not all selected packs could be confirmed uninstalled. Refresh the installed list."
                )
        op.message = f"Uninstalled {len(packs)} packs. No stickers were deleted."

    async def install(self, op, source):
        collection = await self.catalog.load(source)
        require_owned(collection)
        await self.ensure_installed(op, collection.pack)
        op.message = "Pack installed."

    async def rename(self, op, source, title):
        ref = pack_ref((await self.catalog.load(source)).pack)

        async def guard():
            require_owned(await self.catalog.load(source))

        async def verify():
            return (await self.catalog.load(source)).pack.title == title

        await self.rpc.write(
            op,
            api.stickers.RenameStickerSetRequest(ref, title),
            reconcile=verify,
            guard=guard,
        )
        op.message = "Pack renamed."

    async def reorder(self, op, source, before, order, moved):
        async def current():
            if source == "installed":
                return [p.id for p in await self.catalog.installed()], None
            collection = await self.catalog.load(source)
            require_owned(collection)
            return list(collection.docs), collection

        async def guard():
            actual, collection = await current()
            if actual != before:
                raise StickerError(
                    "Telegram’s order changed. Refreshed the list; move the item again."
                )
            return collection

        async def verify():
            actual, _ = await current()
            return actual == order

        collection = await guard()
        if order == before:
            op.message = "Order unchanged."
            return
        if source == "installed":
            request = api.messages.ReorderStickerSetsRequest(order)
        else:
            expected = before.copy()
            expected.remove(moved)
            expected.insert(order.index(moved), moved)
            if expected != order:
                raise StickerError("Move one sticker at a time.")
            doc = collection.docs[moved]
            if not belongs(doc, collection.pack):
                raise StickerError(
                    "This sticker does not belong to the selected owned pack."
                )
            request = api.stickers.ChangeStickerPositionRequest(
                utils.get_input_document(doc), order.index(moved)
            )
        try:
            await self.rpc.write(op, request, reconcile=verify, guard=guard)
        except FILE_REFERENCE_ERRORS as error:
            raise rpc_error(error) from error
        if not await verify():
            raise Uncertain(
                "The new order could not be confirmed. Refreshed the list from Telegram."
            )
        op.message = "Order saved to Telegram."

    async def edit(self, op, action, entries):
        if action == "remove":
            groups = defaultdict(set)
            for source, doc_id in entries:
                groups[source].add(doc_id)
            for source, ids in groups.items():
                state = await self.catalog.load(source)
                require_owned(state)
                if not state.docs.keys() - ids:
                    raise StickerError(
                        "Removing these stickers would empty an owned pack. You can uninstall the pack instead."
                    )

        completed, failures, retry, removed = 0, [], [], []
        uncertain = False
        sources = {}
        for source, doc_id in entries:
            op.message = (
                f"Applying change {completed + len(failures) + 1} of {len(entries)}…"
            )
            list_source = (
                "favorites"
                if action in {"favorite", "unfavorite"}
                else "recent"
                if action == "forget"
                else source
            )
            present = action == "favorite"

            async def verify():
                return (
                    doc_id in (await self.catalog.load(list_source)).docs
                ) == present

            try:
                for attempt in range(2):
                    if action == "remove" or source not in sources:
                        sources[source] = await self.catalog.load(source)
                    collection = sources[source]
                    doc = collection.docs.get(doc_id)
                    if action == "remove":
                        require_owned(collection)
                        if doc and (
                            not belongs(doc, collection.pack)
                            or len(collection.docs) < 2
                        ):
                            raise StickerError(
                                "Cannot remove this sticker. Leave at least one sticker in the owned pack; uninstall it instead."
                            )
                    if not doc:
                        if present:
                            raise StickerError("Sticker is no longer available.")
                        break
                    ref = utils.get_input_document(doc)
                    request = {
                        "remove": lambda: api.stickers.RemoveStickerFromSetRequest(ref),
                        "forget": lambda: api.messages.SaveRecentStickerRequest(
                            ref, unsave=True
                        ),
                        "unfavorite": lambda: api.messages.FaveStickerRequest(
                            ref, unfave=True
                        ),
                        "favorite": lambda: api.messages.FaveStickerRequest(
                            ref, unfave=False
                        ),
                    }[action]()

                    async def guard():
                        fresh = await self.catalog.load(source)
                        require_owned(fresh)
                        if (
                            len(fresh.docs) < 2
                            or not fresh.docs.get(doc_id)
                            or not belongs(fresh.docs[doc_id], fresh.pack)
                        ):
                            raise StickerError(
                                "The owned pack changed. Refresh it before removing stickers."
                            )

                    try:
                        await self.rpc.write(
                            op,
                            request,
                            reconcile=verify,
                            guard=guard if action == "remove" else None,
                        )
                        break
                    except FILE_REFERENCE_ERRORS as error:
                        if attempt:
                            raise rpc_error(error) from error
                        sources.pop(source, None)
                completed += 1
                if action == "remove":
                    removed.append(f"{source}/{doc_id}")
            except StickerError as error:
                failures.append({"id": str(doc_id), "reason": str(error)})
                if isinstance(error, Uncertain):
                    uncertain = True
                else:
                    retry.append((source, doc_id))
        op.result.update(completed=completed, failures=failures, removed=removed)
        if retry:
            op.retry = dict(action=action, entries=retry)
        if uncertain:
            raise Uncertain(
                f"Changed {completed} of {len(entries)} stickers. Some outcomes could not be confirmed."
            )
        if failures:
            raise StickerError(f"Changed {completed} of {len(entries)} stickers.")
        op.message = f"Updated {completed} stickers."
