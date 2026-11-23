import asyncio
import unittest
from collections import defaultdict

from telethon import errors, functions as api
from telethon.network.requeststate import RequestState

from src.adapters.telegram.media import MediaManager
from src.adapters.telegram.recovering_client import _WriteAwarePacker
from src.adapters.telegram.sticker_catalog import belongs
from src.adapters.telegram.stickers import StickerWorkspace
from src.infrastructure.telegram_queue import TelegramWriteQueue
from tests.sticker_fixture import BASE_ID, FakeTelegram, fixture_app


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
