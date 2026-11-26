import asyncio
import unittest
from collections import defaultdict
from copy import copy
from io import BytesIO

from PIL import Image
from telethon import errors, functions as api
from telethon import types as tl
from telethon.network.requeststate import RequestState

from src.adapters.telegram.media import MediaManager
from src.adapters.telegram.recovering_client import _WriteAwarePacker
from src.adapters.telegram.sticker_catalog import belongs
from src.adapters.telegram.sticker_rpc import StickerError
from src.adapters.telegram.stickers import StickerWorkspace
from src.infrastructure.telegram_queue import TelegramWriteQueue
from tests.sticker_fixture import BASE_ID, FakeTelegram, art, fixture_app


class StickerTransport(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_packer_drops_only_marked_writes(self):
        packer = _WriteAwarePacker(None, defaultdict(lambda: None))
        ordinary = RequestState(api.messages.GetAllStickersRequest(0))
        request = api.messages.ReorderStickerSetsRequest([2, 1])
        request._send_once = True
        write = RequestState(request)
        packer.extend([ordinary, write])
        self.assertEqual(list(packer._deque), [ordinary])
        with self.assertRaises(ConnectionError):
            await write.future


class StickerThumbnails(unittest.IsolatedAsyncioTestCase):
    async def test_warm_pack_checks_hash_but_expired_reference_reads_full_pack(self):
        hashes = []
        validating, release = asyncio.Event(), asyncio.Event()

        class VersionedTelegram(FakeTelegram):
            async def __call__(self, request):
                if isinstance(request, api.messages.GetStickerSetRequest):
                    hashes.append(request.hash)
                    if request.hash == self.packs[pack].hash:
                        if request.hash == 43:
                            validating.set()
                            await release.wait()
                        return tl.messages.StickerSetNotModified()
                return await super().__call__(request)

            def details(self, pack_id):
                result = super().details(pack_id)
                result.documents = [copy(doc) for doc in result.documents]
                return result

            async def download_media(self, document, file, thumb=None):
                if document.file_reference != self.docs[document.id].file_reference:
                    raise errors.FileReferenceExpiredError(None)
                return await super().download_media(document, file, thumb)

        fake = VersionedTelegram(count=2)
        (pack,) = fake.installed
        fake.packs[pack].hash = 42
        first, second = fake.grouped[pack][:2]
        app = fixture_app(fake)
        client = app.test_client()
        headers = {"X-Account-Token": app.tg_adapter.account_token}
        items = f"/api/stickers/items/{pack}"
        async with app.test_app(), asyncio.timeout(5):
            await client.get("/api/stickers/packs", headers=headers)
            cold = await client.get(items, headers=headers)
            warm = await client.get(items, headers=headers)
            self.assertEqual(await warm.get_json(), await cold.get_json())
            self.assertEqual(hashes, [0, 42])

            fake.docs[first].file_reference = b"renewed"
            artwork = await client.get(
                f"/api/stickers/art/{pack}/{first}/display", headers=headers
            )
            self.assertEqual(await artwork.get_data(), fake.thumbs[first])
            self.assertEqual(hashes, [0, 42, 0])

            fake.grouped[pack].append(fake.grouped[pack].pop(0))
            fake.packs[pack].hash = 43
            reordered = await client.get(items, headers=headers)
            self.assertEqual((await reordered.get_json())[0]["id"], str(second))
            self.assertEqual(hashes, [0, 42, 0, 42])

            pending = asyncio.create_task(client.get(items, headers=headers))
            try:
                await validating.wait()
                fake.grouped[pack].insert(0, fake.grouped[pack].pop())
                fake.packs[pack].hash = 44
                await client.get("/api/stickers/packs", headers=headers)
            finally:
                release.set()
                latest = await pending
            self.assertEqual((await latest.get_json())[0]["id"], str(first))
            self.assertEqual(hashes, [0, 42, 0, 42, 43, 0])

    async def test_warm_pack_validations_share_a_read_but_not_with_reference_refresh(
        self,
    ):
        blocked, release = asyncio.Event(), asyncio.Event()
        other_joined = asyncio.Event()
        hashes = []

        class VersionedTelegram(FakeTelegram):
            async def __call__(self, request):
                if isinstance(request, api.messages.GetStickerSetRequest):
                    hashes.append(request.hash)
                    if request.hash:
                        blocked.set()
                        await release.wait()
                        return tl.messages.StickerSetNotModified()
                    result = await super().__call__(request)
                    result.documents = [copy(doc) for doc in result.documents]
                    return result
                return await super().__call__(request)

            async def download_media(self, document, file, thumb=None):
                if document.file_reference != self.docs[document.id].file_reference:
                    raise errors.FileReferenceExpiredError(None)
                return await super().download_media(document, file, thumb)

        fake = VersionedTelegram(count=2)
        (pack,) = fake.installed
        fake.packs[pack].hash = 42
        doc = fake.grouped[pack][0]
        app = fixture_app(fake)
        client = app.test_client()
        catalog = app.tg_adapter.stickers._catalog
        headers = {"X-Account-Token": app.tg_adapter.account_token}
        items = f"/api/stickers/items/{pack}"
        original = catalog._load_shared
        validations = 0

        async def note_join(source, cached=None):
            nonlocal validations
            if cached is not None:
                validations += 1
                if validations == 2:
                    other_joined.set()
            return await original(source, cached)

        catalog._load_shared = note_join
        async with app.test_app(), asyncio.timeout(5), asyncio.TaskGroup() as tasks:
            await client.get("/api/stickers/packs", headers=headers)
            cold = await client.get(items, headers=headers)
            first = tasks.create_task(client.get(items, headers=headers))
            await blocked.wait()
            second = tasks.create_task(client.get(items, headers=headers))
            try:
                await other_joined.wait()
                fake.docs[doc].file_reference = b"renewed"
                artwork = await client.get(
                    f"/api/stickers/art/{pack}/{doc}/display", headers=headers
                )
                self.assertEqual(await artwork.get_data(), fake.thumbs[doc])
                self.assertEqual(hashes, [0, 42, 0])
            finally:
                release.set()
            for response in (await first, await second):
                self.assertEqual(await response.get_json(), await cold.get_json())
        self.assertEqual(hashes, [0, 42, 0])

    async def test_warm_pack_validation_survives_lru_eviction(self):
        blocked, release = asyncio.Event(), asyncio.Event()
        hashes = []

        class VersionedTelegram(FakeTelegram):
            async def __call__(self, request):
                if (
                    isinstance(request, api.messages.GetStickerSetRequest)
                    and self.set_id(request.stickerset) == pack
                ):
                    hashes.append(request.hash)
                    if request.hash:
                        blocked.set()
                        await release.wait()
                        return tl.messages.StickerSetNotModified()
                return await super().__call__(request)

        fake = VersionedTelegram(count=34)
        fake.delay = 0
        pack, *others = fake.installed
        fake.packs[pack].hash = 42
        app = fixture_app(fake)
        catalog = app.tg_adapter.stickers._catalog
        async with app.test_app(), asyncio.timeout(5), asyncio.TaskGroup() as tasks:
            await catalog.summaries()
            cold = await catalog.items(str(pack))
            warm = tasks.create_task(catalog.items(str(pack)))
            try:
                await blocked.wait()
                for other in others:
                    await catalog.items(str(other))
            finally:
                release.set()
            self.assertEqual(await warm, cold)
        self.assertEqual(hashes, [0, 42])

    async def test_not_modified_does_not_restore_a_replaced_pack(self):
        reading, read_details, checked, reply = (asyncio.Event() for _ in range(4))
        refreshing = False

        class VersionedTelegram(FakeTelegram):
            async def __call__(self, request):
                if refreshing and isinstance(
                    request, api.messages.GetStickerSetRequest
                ):
                    if request.hash:
                        checked.set()
                        await reply.wait()
                        return tl.messages.StickerSetNotModified()
                    reading.set()
                    await read_details.wait()
                return await super().__call__(request)

        fake = VersionedTelegram(count=2)
        (pack,) = fake.installed
        fake.packs[pack].hash = 42
        app = fixture_app(fake)
        client = app.test_client()
        catalog = app.tg_adapter.stickers._catalog
        headers = {"X-Account-Token": app.tg_adapter.account_token}
        path = f"/api/stickers/items/{pack}"
        async with app.test_app(), asyncio.timeout(5), asyncio.TaskGroup() as tasks:
            await client.get("/api/stickers/packs", headers=headers)
            await client.get(path, headers=headers)
            refreshing = True
            full = tasks.create_task(catalog.load(str(pack)))
            await reading.wait()
            conditional = tasks.create_task(client.get(path, headers=headers))
            await checked.wait()
            fake.grouped[pack].pop(0)
            fake.packs[pack].hash = 43
            read_details.set()
            await full
            reply.set()
            response = await conditional
            self.assertEqual(
                [item["id"] for item in await response.get_json()],
                list(map(str, fake.grouped[pack])),
            )

    async def test_partial_artwork_is_not_served_and_can_be_retried(self):
        fake = FakeTelegram(count=2)
        (pack,) = fake.installed
        doc = fake.grouped[pack][0]
        app = fixture_app(fake)
        client = app.test_client()
        headers = {"X-Account-Token": app.tg_adapter.account_token}
        async with app.test_app():
            await client.get("/api/stickers/packs", headers=headers)
            for mode, artwork in (("full", fake.full), ("thumb", fake.thumbs)):
                with self.subTest(mode=mode):
                    path = f"/api/stickers/art/{pack}/{doc}/{mode}"
                    original = artwork[doc]
                    artwork[doc] = original[:-10]
                    partial = await client.get(path, headers=headers)
                    artwork[doc] = original
                    complete = await client.get(path, headers=headers)
                    self.assertEqual(partial.status_code, 404)
                    self.assertEqual(await complete.get_data(), original)

    async def test_expired_reference_does_not_break_another_sources_artwork(self):
        started, joined, release = (asyncio.Event() for _ in range(3))

        class ExpiredFavorite(FakeTelegram):
            pack_read_unavailable = False

            async def __call__(self, request):
                if self.pack_read_unavailable and isinstance(
                    request, api.messages.GetStickerSetRequest
                ):
                    raise OSError("Pack lookup temporarily unavailable")
                result = await super().__call__(request)
                if isinstance(request, api.messages.GetFavedStickersRequest):
                    result.stickers = [copy(doc) for doc in result.stickers]
                    for doc in result.stickers:
                        doc.file_reference = b"expired"
                return result

            async def download_media(self, document, file, thumb=None):
                if document.file_reference == b"expired":
                    started.set()
                    await release.wait()
                    raise errors.FileReferenceExpiredError(None)
                return await super().download_media(document, file, thumb)

        fake = ExpiredFavorite(count=2)
        (pack,) = fake.installed
        doc_id = fake.grouped[pack][0]
        fake.favorites = [doc_id]
        app = fixture_app(fake)
        client = app.test_client()
        headers = {"X-Account-Token": app.tg_adapter.account_token}
        media = app.tg_adapter.stickers._media
        transfer = media.transfer

        async def note_join(source, doc, mode):
            if source == str(pack):
                joined.set()
            return await transfer(source, doc, mode)

        async def get_artwork(source):
            return await client.get(
                f"/api/stickers/art/{source}/{doc_id}/display", headers=headers
            )

        media.transfer = note_join
        async with app.test_app(), asyncio.timeout(5), asyncio.TaskGroup() as tasks:
            await client.get("/api/stickers/packs", headers=headers)
            await client.get(f"/api/stickers/items/{pack}", headers=headers)
            await client.get("/api/stickers/items/favorites", headers=headers)
            fake.pack_read_unavailable = True
            favorite = tasks.create_task(get_artwork("favorites"))
            try:
                await started.wait()
                fake.favorites.clear()
                installed = tasks.create_task(get_artwork(pack))
                await joined.wait()
            finally:
                release.set()
            self.assertEqual((await favorite).status_code, 400)
            response = await installed
            self.assertEqual(response.status_code, 200)
            self.assertEqual(await response.get_data(), fake.thumbs[doc_id])
            self.assertEqual(fake.counts["thumbnail"], 1)

    async def test_cached_cover_outlives_pack_details_but_not_pack_changes(self):
        fake = FakeTelegram(count=34)
        fake.delay = 0
        pack, *others = fake.installed
        first, second = fake.grouped[pack][:2]
        app = fixture_app(fake)
        client = app.test_client()
        headers = {"X-Account-Token": app.tg_adapter.account_token}
        packs = "/api/stickers/packs"
        cover = f"/api/stickers/art/{pack}/display"

        async with app.test_app():
            await client.get(packs, headers=headers)
            initial = await client.get(cover, headers=headers)
            self.assertEqual(await initial.get_data(), fake.thumbs[first])
            for other in others:
                await client.get(f"/api/stickers/items/{other}", headers=headers)
            fake.counts.clear()

            await client.get(packs, headers=headers)
            warm = await client.get(cover, headers=headers)
            self.assertEqual(await warm.get_data(), fake.thumbs[first])
            self.assertEqual(fake.counts["GetStickerSetRequest"], 0)

            fake.grouped[pack].append(fake.grouped[pack].pop(0))
            fake.packs[pack].hash += 1
            await client.get(packs, headers=headers)
            changed = await client.get(cover, headers=headers)
            self.assertEqual(await changed.get_data(), fake.thumbs[second])
            self.assertEqual(fake.counts["GetStickerSetRequest"], 1)

            fake.installed.remove(pack)
            refreshed = await client.get(packs, headers=headers)
            self.assertNotIn(
                str(pack), {row["id"] for row in await refreshed.get_json()}
            )
            self.assertEqual(
                (await client.get(cover, headers=headers)).status_code, 404
            )
            self.assertEqual(fake.counts["GetStickerSetRequest"], 1)

    async def test_cover_uses_first_sticker_and_shares_the_image_cache(self):
        fake = FakeTelegram(count=3)
        fake.delay = 0
        pack, empty = fake.installed
        first, second = fake.grouped[pack][:2]
        fake.packs[pack].thumb_document_id = second
        fake.grouped[empty].clear()
        fake.docs[second].thumbs = []
        fake.full[second] = art(8, size=512)
        fake.docs[second].size = len(fake.full[second])
        app = fixture_app(fake)
        client = app.test_client()
        headers = {"X-Account-Token": app.tg_adapter.account_token}
        cover_path = f"/api/stickers/art/{pack}/display"

        async def get_art(ref, mode="display"):
            response = await client.get(
                f"/api/stickers/art/{ref}/{mode}", headers=headers
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "image/webp")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return await response.get_data()

        async with app.test_app():
            await client.get("/api/stickers/packs", headers=headers)
            self.assertEqual((await client.get(cover_path)).status_code, 409)
            cover, first_still, second_still, original = await asyncio.gather(
                get_art(pack),
                get_art(f"{pack}/{first}"),
                get_art(f"{pack}/{second}"),
                get_art(f"{pack}/{second}", "full"),
            )
            self.assertEqual(cover, fake.thumbs[first])
            self.assertEqual(cover, first_still)
            self.assertEqual(original, fake.full[second])
            with Image.open(BytesIO(second_still)) as image:
                self.assertEqual((image.format, image.size), ("WEBP", (192, 192)))
            self.assertEqual(await get_art(f"{pack}/{second}"), second_still)
            missing = await client.get(
                f"/api/stickers/art/{empty}/display", headers=headers
            )
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(fake.counts["thumbnail"], 1)
            self.assertEqual(fake.counts["original"], 1)
            self.assertEqual(fake.counts["GetStickerSetRequest"], 2)
            # A pack edit invalidates the cached order, not the immutable artwork.
            fake.grouped[pack].remove(first)
            await client.get("/api/stickers/packs", headers=headers)
            self.assertEqual(await get_art(pack), second_still)
            self.assertEqual(fake.counts["original"], 1)

    async def test_display_falls_back_to_available_webp_artwork(self):
        class MissingArtwork(FakeTelegram):
            async def download_media(self, document, file, thumb=None):
                if document.id == first and thumb is None:
                    return None
                if document.id == third and thumb is not None:
                    raise errors.LocationInvalidError(None)
                return await super().download_media(document, file, thumb)

        fake = MissingArtwork(count=2)
        fake.delay = 0
        (pack,) = fake.installed
        first, second, third = fake.grouped[pack][:3]
        fake.docs[first].thumbs = [tl.PhotoSize("s", 64, 64, len(fake.thumbs[first]))]
        fake.thumbs[second] = b"not an image"
        fake.docs[second].thumbs[0].size = len(fake.thumbs[second])
        app = fixture_app(fake)
        client = app.test_client()
        headers = {"X-Account-Token": app.tg_adapter.account_token}
        async with app.test_app():
            await client.get("/api/stickers/packs", headers=headers)
            for doc_id, expected in (
                (first, fake.thumbs[first]),
                (second, fake.full[second]),
                (third, fake.full[third]),
            ):
                with self.subTest(doc_id=doc_id):
                    response = await client.get(
                        f"/api/stickers/art/{pack}/{doc_id}/display",
                        headers=headers,
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(await response.get_data(), expected)

    async def test_items_rechecks_account_after_shared_load(self):
        fake = FakeTelegram(count=2)
        (pack,) = fake.installed
        app = fixture_app(fake)
        client = app.test_client()
        workspace = app.tg_adapter.stickers
        headers = {"X-Account-Token": app.tg_adapter.account_token}
        remember = workspace._catalog.remember

        def retire_on_completion(source, result, *, revision=None):
            remember(source, result, revision=revision)
            asyncio.get_running_loop().call_soon(workspace.retire)

        async with app.test_app():
            await client.get("/api/stickers/packs", headers=headers)
            workspace._catalog.remember = retire_on_completion
            response = await client.get(f"/api/stickers/items/{pack}", headers=headers)
            self.assertEqual(response.status_code, 409)

    async def test_items_returns_account_conflict_if_shared_load_never_starts(self):
        fake = FakeTelegram(count=2)
        (pack,) = fake.installed
        app = fixture_app(fake)
        client = app.test_client()
        workspace = app.tg_adapter.stickers
        headers = {"X-Account-Token": app.tg_adapter.account_token}
        load = workspace._catalog._load_shared

        async def retire_before_load(source):
            asyncio.get_running_loop().call_soon(workspace.retire)
            return await load(source)

        async with app.test_app(), asyncio.timeout(2):
            await client.get("/api/stickers/packs", headers=headers)
            workspace._catalog._load_shared = retire_before_load
            response = await client.get(f"/api/stickers/items/{pack}", headers=headers)
            self.assertEqual(response.status_code, 409)

    async def test_account_change_during_cover_download_returns_conflict(self):
        started = asyncio.Event()

        class PendingDownload(FakeTelegram):
            async def download_media(self, document, file, thumb=None):
                started.set()
                await asyncio.Future()

        fake = PendingDownload(count=2)
        (pack,) = fake.installed
        app = fixture_app(fake)
        client = app.test_client()
        workspace = app.tg_adapter.stickers
        headers = {"X-Account-Token": app.tg_adapter.account_token}
        async with app.test_app(), asyncio.timeout(2):
            await client.get("/api/stickers/packs", headers=headers)
            request = asyncio.create_task(
                client.get(f"/api/stickers/art/{pack}/display", headers=headers)
            )
            await started.wait()
            workspace.retire()
            await workspace._media.media.close()
            self.assertEqual((await request).status_code, 409)

    async def test_late_pack_summary_does_not_invalidate_newer_artwork(self):
        for refresh_packs in (True, False):
            with self.subTest(refresh_packs=refresh_packs):
                await self.check_late_summary(refresh_packs)

    async def test_newer_summary_invalidates_cover_loaded_during_refresh(self):
        details_ready, release_details, summary_ready, release_summary = (
            asyncio.Event() for _ in range(4)
        )
        refreshing = False

        class DelayedTelegram(FakeTelegram):
            async def __call__(self, request):
                result = await super().__call__(request)
                if (
                    isinstance(request, api.messages.GetStickerSetRequest)
                    and not details_ready.is_set()
                ):
                    details_ready.set()
                    await release_details.wait()
                if refreshing and isinstance(
                    request, api.messages.GetAllStickersRequest
                ):
                    summary_ready.set()
                    await release_summary.wait()
                return result

        fake = DelayedTelegram(count=2)
        (pack,) = fake.installed
        first, second = fake.grouped[pack][:2]
        app = fixture_app(fake)
        client = app.test_client()
        headers = {"X-Account-Token": app.tg_adapter.account_token}
        packs = "/api/stickers/packs"
        cover = f"/api/stickers/art/{pack}/display"
        async with app.test_app(), asyncio.timeout(5), asyncio.TaskGroup() as tasks:
            await client.get(packs, headers=headers)
            pending_cover = tasks.create_task(client.get(cover, headers=headers))
            await details_ready.wait()
            fake.grouped[pack].remove(first)
            refreshing = True
            pending_summary = tasks.create_task(client.get(packs, headers=headers))
            await summary_ready.wait()
            release_details.set()
            self.assertEqual(await (await pending_cover).get_data(), fake.thumbs[first])
            release_summary.set()
            fresh = await pending_summary
            self.assertEqual((await fresh.get_json())[0]["count"], 99)
            current = await client.get(cover, headers=headers)
            self.assertEqual(await current.get_data(), fake.thumbs[second])

    async def check_late_summary(self, refresh_packs):
        started, release = asyncio.Event(), asyncio.Event()
        delay_summary = False

        class DelayedTelegram(FakeTelegram):
            async def __call__(self, request):
                result = await super().__call__(request)
                if (
                    delay_summary
                    and isinstance(request, api.messages.GetAllStickersRequest)
                    and not started.is_set()
                ):
                    started.set()
                    await release.wait()
                return result

        fake = DelayedTelegram(count=2)
        (pack,) = fake.installed
        app = fixture_app(fake)
        client = app.test_client()
        headers = {"X-Account-Token": app.tg_adapter.account_token}
        packs = "/api/stickers/packs"
        items = f"/api/stickers/items/{pack}"
        cover = f"/api/stickers/art/{pack}/display"
        async with app.test_app(), asyncio.timeout(5):
            await client.get(packs, headers=headers)
            delay_summary = True
            earlier = asyncio.create_task(client.get(packs, headers=headers))
            try:
                await started.wait()
                fake.grouped[pack].pop(0)
                if refresh_packs:
                    fresh = await client.get(packs, headers=headers)
                    self.assertEqual((await fresh.get_json())[0]["count"], 99)
                    self.assertEqual(
                        (await client.get(cover, headers=headers)).status_code, 200
                    )
                else:
                    fresh = await client.get(items, headers=headers)
                    self.assertEqual(len(await fresh.get_json()), 99)
            finally:
                release.set()
                late = await earlier
            self.assertEqual((await late.get_json())[0]["count"], 99)
            current = await client.get(cover, headers=headers)
            self.assertEqual(
                await current.get_data(), fake.thumbs[fake.grouped[pack][0]]
            )
            self.assertEqual(fake.counts["GetStickerSetRequest"], 1)

    async def test_refresh_does_not_reuse_pending_cover_lookup(self):
        for direct in (False, True):
            for rename in (False, True):
                with self.subTest(direct=direct, rename=rename):
                    await self.check_pending_lookup(direct, rename)

    async def test_recent_refresh_keeps_newest_list(self):
        for artwork in (False, True):
            with self.subTest(artwork=artwork):
                await self.check_recent_refresh(artwork)

    async def check_recent_refresh(self, artwork):
        started, release = asyncio.Event(), asyncio.Event()

        class DelayedTelegram(FakeTelegram):
            async def __call__(self, request):
                result = await super().__call__(request)
                if (
                    isinstance(request, api.messages.GetRecentStickersRequest)
                    and not started.is_set()
                ):
                    started.set()
                    await release.wait()
                return result

        fake = DelayedTelegram(count=2)
        app = fixture_app(fake)
        client = app.test_client()
        headers = {"X-Account-Token": app.tg_adapter.account_token}
        removed, retained = fake.recent
        art_path = f"/api/stickers/art/recent/{removed}/display"
        list_path = "/api/stickers/items/recent"
        async with app.test_app():
            old = asyncio.create_task(
                client.get(art_path if artwork else list_path, headers=headers)
            )
            try:
                async with asyncio.timeout(2):
                    await started.wait()
                    fake.recent.remove(removed)
                    fresh = await client.get(list_path, headers=headers)
                    self.assertEqual(
                        [item["id"] for item in await fresh.get_json()], [str(retained)]
                    )
            finally:
                release.set()
                older_response = await old
            if artwork:
                self.assertEqual(older_response.status_code, 404)
            else:
                self.assertEqual(
                    await older_response.get_json(), await fresh.get_json()
                )
            self.assertEqual(
                (await client.get(art_path, headers=headers)).status_code, 404
            )

    async def check_pending_lookup(self, direct, rename):
        started, release = asyncio.Event(), asyncio.Event()

        class DelayedTelegram(FakeTelegram):
            async def __call__(self, request):
                result = await super().__call__(request)
                if (
                    isinstance(request, api.messages.GetStickerSetRequest)
                    and not started.is_set()
                ):
                    started.set()
                    await release.wait()
                return result

        fake = DelayedTelegram(count=3)
        pack = fake.installed[0]
        removed = fake.grouped[pack][0]
        app = fixture_app(fake)
        client = app.test_client()
        headers = {"X-Account-Token": app.tg_adapter.account_token}
        packs = "/api/stickers/packs"
        cover = f"/api/stickers/art/{pack}/display"
        async with app.test_app(), asyncio.timeout(5):
            self.assertEqual(
                (await client.get(packs, headers=headers)).status_code, 200
            )
            old = asyncio.create_task(
                app.tg_adapter.stickers._catalog.load(str(pack))
                if direct
                else client.get(cover, headers=headers)
            )
            try:
                await started.wait()
                if rename:
                    fake.packs[pack].title = "Renamed pack"
                else:
                    fake.grouped[pack].remove(removed)
                refreshed = await client.get(packs, headers=headers)
                summary = (await refreshed.get_json())[0]
                self.assertEqual(summary["count"], 100 if rename else 99)
                if rename:
                    self.assertEqual(summary["name"], "Renamed pack")
                items = await client.get(f"/api/stickers/items/{pack}", headers=headers)
                self.assertEqual(
                    [item["id"] for item in await items.get_json()],
                    list(map(str, fake.grouped[pack])),
                )
                self.assertFalse(old.done())
            finally:
                release.set()
                if direct:
                    with self.assertRaisesRegex(
                        StickerError, "Pack changed while loading"
                    ):
                        await old
                else:
                    self.assertEqual((await old).status_code, 400)
            current = await client.get(cover, headers=headers)
            first = fake.grouped[pack][0]
            self.assertEqual(await current.get_data(), fake.thumbs[first])
            if rename:
                catalog = app.tg_adapter.stickers._catalog
                self.assertEqual(catalog.known_pack(str(pack)).title, "Renamed pack")


class StickerFollowUps(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_install_can_be_retried_but_timeout_cannot(self):
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                await self.check_install_retry(ambiguous)

    async def check_install_retry(self, ambiguous):
        fake = FakeTelegram(count=3)
        app = fixture_app(fake)
        workspace = app.tg_adapter.stickers
        source = fake.installed[0]
        fake.grouped[source] = fake.grouped[source][:1]
        client = app.test_client()
        headers = {
            "X-Account-Token": workspace.token,
            "X-Requested-With": "XMLHttpRequest",
        }
        base = "/api/stickers/operations"

        async def start(path, body):
            response = await client.post(path, json=body, headers=headers)
            self.assertEqual(response.status_code, 202, await response.get_data())
            op_id = (await response.get_json())["id"]
            async with asyncio.timeout(5):
                await workspace.operation(op_id).task
            return op_id

        async def status(op_id):
            return await (
                await client.get(f"{base}/{op_id}", headers=headers)
            ).get_json()

        async with app.test_app():
            await client.get("/api/stickers/packs", headers=headers)
            copy = await start(
                base,
                dict(
                    kind="copy",
                    entries=[[str(source), str(fake.grouped[source][0])]],
                    title="Replacement",
                    slug="replacement",
                ),
            )
            self.assertTrue((await status(copy))["can_install"])
            action = "InstallStickerSetRequest"
            if ambiguous:
                send = fake.send_once

                async def timeout(request):
                    if isinstance(request, api.messages.InstallStickerSetRequest):
                        fake.counts[action] += 1
                        raise TimeoutError
                    return await send(request)

                fake.send_once = timeout
            else:
                fake.fail_at = action
            failed = await start(f"{base}/{copy}/install", {})
            self.assertFalse((await status(copy)).get("can_install"))
            state = await status(failed)
            self.assertEqual(fake.counts[action], 1)
            if ambiguous:
                self.assertEqual(state["state"], "uncertain")
                self.assertFalse(state.get("can_install"))
                response = await client.post(
                    f"{base}/{failed}/install", json={}, headers=headers
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(fake.counts[action], 1)
                return
            self.assertEqual(state["state"], "failed")
            self.assertTrue(state["can_install"])
            installed = await start(f"{base}/{failed}/install", {})
            state = await status(installed)
            self.assertEqual(state["state"], "done", state["message"])
            self.assertIn("uninstall_original", state)
            self.assertIn(source, fake.installed)
            self.assertEqual(fake.counts[action], 2)


class StickerOperations(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeTelegram(count=3)
        self.fake.delay = 0
        self.media = MediaManager(self.fake)
        self.queue = TelegramWriteQueue()
        self.workspace = StickerWorkspace(self.fake, self.media, self.queue)
        await self.queue.start()
        await self.workspace.summaries()
        self.source, self.owned = BASE_ID + 1, BASE_ID + 2

    async def asyncTearDown(self):
        self.workspace.retire()
        await self.workspace.close()
        await self.queue.stop()
        await self.media.close()

    async def run_operation(self, kind, **parameters):
        op = self.workspace.operation(self.workspace.start(kind, parameters))
        async with asyncio.timeout(5):
            await op.task
        return op

    def entries(self, source, count=3):
        return [(str(source), doc_id) for doc_id in self.fake.grouped[source][:count]]

    def copy_parameters(self):
        return dict(
            entries=self.entries(self.source),
            destination=None,
            title="Reactions",
            slug="reactions",
            install=True,
        )

    async def test_copy_and_explicit_uninstall(self):
        self.fake.grouped[self.source] = self.fake.grouped[self.source][:3]
        self.fake.land_timeout = "AddStickerToSetRequest"
        op = await self.run_operation("copy", **self.copy_parameters())
        self.assertEqual(op.state, "done", op.message)
        dest = int(op.result["destination"])
        originals = self.fake.grouped[self.source][:3]
        copied = self.fake.grouped[dest]
        self.assertEqual(
            [self.fake.full[i] for i in copied], [self.fake.full[i] for i in originals]
        )
        self.assertTrue(
            all(belongs(self.fake.docs[i], self.fake.packs[dest]) for i in copied)
        )
        self.assertTrue(set(copied).isdisjoint(originals))
        self.assertEqual(self.fake.counts["AddStickerToSetRequest"], 2)
        self.assertIn("uninstall_original", op.result)
        self.assertIn(self.source, self.fake.installed)
        self.assertIn(dest, self.fake.installed)
        follow = await self.run_operation(
            "finish_copy", replacement=op.replacement, uninstall=True
        )
        self.assertEqual(follow.state, "done", follow.message)
        self.assertNotIn(self.source, self.fake.installed)
        self.assertEqual(self.fake.grouped[self.source], originals)

    async def test_copying_selected_stickers_cannot_uninstall_the_pack(self):
        op = await self.run_operation("copy", **self.copy_parameters())
        self.assertEqual(op.state, "done", op.message)
        self.assertNotIn("uninstall_original", op.result)
        follow = await self.run_operation(
            "finish_copy", replacement=op.replacement, uninstall=True
        )
        self.assertEqual(follow.state, "failed")
        self.assertIn(self.source, self.fake.installed)
        self.assertEqual(self.fake.counts["ToggleStickerSetsRequest"], 0)

    async def test_original_uninstall_revalidates_source(self):
        extra = self.fake.grouped[self.source][3]
        self.fake.grouped[self.source] = self.fake.grouped[self.source][:3]
        op = await self.run_operation("copy", **self.copy_parameters())
        self.assertIn("uninstall_original", op.result)
        self.fake.grouped[self.source].append(extra)
        follow = await self.run_operation(
            "finish_copy", replacement=op.replacement, uninstall=True
        )
        self.assertEqual(follow.state, "failed")
        self.assertIn(self.source, self.fake.installed)
        self.assertEqual(self.fake.counts["ToggleStickerSetsRequest"], 0)

    async def test_sticker_flood_wait_releases_worker_and_retry_checks_guard(self):
        rejected, unrelated = asyncio.Event(), asyncio.Event()
        send_once = self.fake.send_once

        async def rate_limited(request):
            if not rejected.is_set():
                rejected.set()
                raise errors.FloodWaitError(request, capture=1)
            return await send_once(request)

        async def change_order():
            self.fake.installed.reverse()
            unrelated.set()

        self.fake.send_once = rate_limited
        before = self.fake.installed.copy()
        op = self.workspace.operation(
            self.workspace.start(
                "reorder",
                dict(
                    source="installed",
                    before=before,
                    order=before[::-1],
                    moved=before[0],
                ),
            )
        )
        async with asyncio.timeout(5):
            await rejected.wait()
            await self.queue.enqueue(change_order, automatic=True)
            await unrelated.wait()
            self.assertEqual(op.state, "running")
            self.assertEqual(self.fake.counts["ReorderStickerSetsRequest"], 0)
            await op.task
        self.assertEqual(op.state, "failed")
        self.assertIn("order changed", op.message)
        self.assertEqual(self.fake.counts["ReorderStickerSetsRequest"], 0)

    async def test_partial_copy_and_install_failure(self):
        self.fake.fail_at = "AddStickerToSetRequest"
        partial = await self.run_operation("copy", **self.copy_parameters())
        self.assertEqual(partial.state, "failed")
        self.assertEqual(partial.result["copied"], 1)
        self.assertIsNone(partial.replacement)
        self.assertIn(self.source, self.fake.installed)
        self.fake.fail_at, self.fake.fail_offset = "InstallStickerSetRequest", 1
        failed = await self.run_operation("copy", **partial.retry)
        self.assertEqual(failed.state, "failed")
        self.assertNotIn("uninstall_original", failed.result)
        self.assertIn(self.source, self.fake.installed)
        self.assertNotIn(int(failed.result["destination"]), self.fake.installed)
        self.assertEqual(self.fake.counts["CreateStickerSetRequest"], 1)
        self.assertEqual(len(self.fake.grouped[int(failed.result["destination"])]), 3)

    async def test_batch_favorite_loads_pack_once_and_refreshes_expired_reference(self):
        send_once = self.fake.send_once
        expired = False

        async def send(request):
            nonlocal expired
            if not expired:
                expired = True
                raise errors.FileReferenceExpiredError(request)
            return await send_once(request)

        self.fake.send_once = send
        op = await self.run_operation(
            "edit", action="favorite", entries=self.entries(self.source)
        )
        self.assertEqual(op.state, "done", op.message)
        self.assertEqual(self.fake.counts["GetStickerSetRequest"], 2)
        self.assertEqual(self.fake.favorites, self.fake.grouped[self.source][:3])

    async def test_interrupted_sticker_rpc_finishes_and_keeps_worker_alive(self):
        async def interrupted(_):
            raise asyncio.CancelledError

        self.fake.send_once = interrupted
        op = await self.run_operation(
            "edit", action="favorite", entries=self.entries(self.source, 1)
        )
        self.assertEqual(op.state, "uncertain")
        self.assertIsNone(op.retry)
        self.assertTrue(self.queue.is_running())

    async def test_exact_and_visual_matches_are_separate(self):
        for pack in (self.source, self.owned):
            self.fake.grouped[pack] = self.fake.grouped[pack][:3]
        op = await self.run_operation(
            "scan", sources=[str(self.source), str(self.owned)]
        )
        self.assertEqual(op.state, "done", op.message)
        a, b = self.fake.grouped[self.source], self.fake.grouped[self.owned]
        self.assertEqual(
            [[int(t["id"]) for t in group] for group in op.result["exact"]],
            [[a[0], b[0]]],
        )
        visual = {
            frozenset(int(t["id"]) for t in pair["items"])
            for pair in op.result["lookalikes"]
        }
        self.assertIn(frozenset((a[1], b[1])), visual)
        self.assertIn(frozenset((a[2], b[2])), visual)
        added = await self.run_operation(
            "copy", **(self.copy_parameters() | {"destination": str(self.owned)})
        )
        self.assertEqual(added.state, "done", added.message)
        self.assertEqual((added.result["copied"], added.result["duplicates"]), (2, 1))
        self.assertEqual(len(self.fake.grouped[self.owned]), 5)

    async def test_scan_artwork_survives_collection_eviction(self):
        self.workspace.retire()
        await self.workspace.close()
        await self.media.close()
        self.fake = fake = FakeTelegram(count=34)
        self.media = MediaManager(fake)
        self.workspace = StickerWorkspace(fake, self.media, self.queue)
        fake.delay = 0
        packs = fake.installed
        first, second = fake.grouped[packs[0]][:2]
        for pack in packs:
            a, b = fake.grouped[pack][:2]
            fake.document(a, fake.full[first], pack, image=fake.thumbs[first])
            fake.document(b, fake.full[second], pack, image=fake.thumbs[second])
            fake.grouped[pack] = [a, b]
        await self.workspace.summaries()
        op = await self.run_operation("scan", sources=list(map(str, packs)))
        self.assertEqual(op.state, "done", op.message)
        source = str(packs[0])
        self.assertTrue(op.result["exact"][0][0]["thumb"])
        fake.counts.clear()
        a, b, wrong = await asyncio.gather(
            self.workspace.artwork(source, first, "thumb"),
            self.workspace.artwork(source, second, "thumb"),
            self.workspace.artwork(source, fake.grouped[packs[-1]][0], "thumb"),
        )
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual((a.data, b.data), (fake.thumbs[first], fake.thumbs[second]))
        self.assertIsNone(wrong)
        self.assertEqual(fake.counts["GetStickerSetRequest"], 1)
        self.assertEqual(fake.counts["thumbnail"], 0)

    async def test_stale_order_and_third_party_edits_send_no_mutation(self):
        before = self.fake.installed.copy()
        self.fake.installed.reverse()
        reorder = await self.run_operation(
            "reorder",
            source="installed",
            before=before,
            order=before[::-1],
            moved=before[0],
        )
        self.assertEqual(reorder.state, "failed")
        self.assertEqual(self.fake.counts["ReorderStickerSetsRequest"], 0)
        removal = await self.run_operation(
            "edit", action="remove", entries=self.entries(self.source, 1)
        )
        self.assertEqual(removal.state, "failed")
        self.assertIn("Only packs you own", removal.message)
        self.assertEqual(self.fake.counts["RemoveStickerFromSetRequest"], 0)

    async def test_original_uninstall_revalidates_destination(self):
        self.fake.grouped[self.source] = self.fake.grouped[self.source][:3]
        op = await self.run_operation("copy", **self.copy_parameters())
        self.assertIn("uninstall_original", op.result)
        self.fake.grouped[int(op.result["destination"])].pop()
        follow = await self.run_operation(
            "finish_copy", replacement=op.replacement, uninstall=True
        )
        self.assertEqual(follow.state, "failed")
        self.assertIn(self.source, self.fake.installed)
        self.assertEqual(self.fake.counts["ToggleStickerSetsRequest"], 0)
