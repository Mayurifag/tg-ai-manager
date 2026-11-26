"""Local browser fixture: uv run --no-sync python -m tests.sticker_fixture."""

import asyncio
from collections import Counter
from copy import copy
from datetime import UTC, datetime
from functools import cache
from io import BytesIO
from types import SimpleNamespace

from PIL import Image, ImageDraw
from quart import jsonify, request
from telethon import functions as api, types as tl

from src.adapters.telegram.media import MediaManager
from src.adapters.telegram.sticker_catalog import sticker_attr
from src.adapters.telegram.stickers import StickerWorkspace
from src.infrastructure.telegram_queue import TelegramWriteQueue
from src.web import create_app

NOW = datetime.now(UTC)
BASE_ID = 7200000000000000000


@cache
def art(index, text=None, size=96, quality=90):
    image = Image.new("RGBA", (96, 96))
    pen = ImageDraw.Draw(image)
    color = (85 + 20 * (index % 8), 55 + 23 * (index % 7), 110 + 11 * (index % 9))
    pen.ellipse((8, 6, 88, 85), fill=color, outline="white", width=3)
    pen.ellipse((26, 25, 33, 34), fill="white")
    pen.ellipse((56, 25, 63, 34), fill="white")
    pen.text((30, 50), text if text is not None else str(index), fill="white")
    with BytesIO() as output:
        image.resize((size, size)).save(output, "WEBP", quality=quality)
        return output.getvalue()


class FakeTelegram:
    def __init__(self, count=100):
        self.counts = Counter()
        self.packs, self.docs, self.full, self.thumbs, self.grouped, self.uploaded = (
            {},
            {},
            {},
            {},
            {},
            {},
        )
        self.favorites, self.recent, self.installed = [], [], []
        self.fail_at = None
        self.fail_offset = 1
        self.land_timeout = None
        self.delay = 0.006
        self.next_id = BASE_ID + 1000000
        examples = [art(i) for i in range(100)]
        for i in range(count):
            pack_id = BASE_ID + i + 1
            owned = i % 2 == 1 or i == count - 1
            self.packs[pack_id] = tl.StickerSet(
                pack_id,
                1,
                f"Pack {i + 1:03}",
                f"pack_{i + 1:03}",
                100,
                0,
                creator=owned,
            )
            self.grouped[pack_id] = []
            if i < count - 1:
                self.installed.append(pack_id)
            for j in range(100):
                doc_id = BASE_ID + 10000 + 100 * i + j
                image = examples[j]
                raw = image + bytes([i]) * (i + 1) if i else image
                if i == 1 and j < 3:
                    image = [
                        examples[0],
                        art(1, size=72, quality=70),
                        art(2, text="NO"),
                    ][j]
                    raw = image
                self.document(doc_id, raw, pack_id, image=image)
                self.grouped[pack_id].append(doc_id)
        self.recent = [BASE_ID + 10001, BASE_ID + 10002]

    def document(self, doc_id, raw, pack_id=None, image=None, emoji="🙂"):
        ref = tl.InputStickerSetID(pack_id, 1) if pack_id else tl.InputStickerSetEmpty()
        self.full[doc_id] = raw
        self.thumbs[doc_id] = raw if image is None else image
        doc = self.docs[doc_id] = tl.Document(
            doc_id,
            1,
            b"ref",
            NOW,
            "image/webp",
            len(raw),
            1,
            [
                tl.DocumentAttributeImageSize(512, 512),
                tl.DocumentAttributeSticker(emoji, ref),
            ],
            thumbs=[tl.PhotoSize("m", 96, 96, len(self.thumbs[doc_id]))],
        )
        return doc

    def new_id(self):
        self.next_id += 1
        return self.next_id

    def set_id(self, ref):
        if isinstance(ref, tl.InputStickerSetID) and ref.id in self.packs:
            return ref.id
        if isinstance(ref, tl.InputStickerSetShortName):
            for pack in self.packs.values():
                if pack.short_name.lower() == ref.short_name.lower():
                    return pack.id
        raise OSError("fixture pack not found")

    def summary(self, pack_id):
        pack = copy(self.packs[pack_id])
        pack.count = len(self.grouped[pack_id])
        return pack

    def details(self, pack_id):
        docs = [self.docs[i] for i in self.grouped[pack_id]]
        return tl.messages.StickerSet(self.summary(pack_id), self.emoji(docs), [], docs)

    @staticmethod
    def emoji(docs):
        return [tl.StickerPack(sticker_attr(doc).alt, [doc.id]) for doc in docs]

    async def __call__(self, req):
        self.counts[type(req).__name__] += 1
        await asyncio.sleep(self.delay)
        match req:
            case api.messages.GetAllStickersRequest():
                return tl.messages.AllStickers(
                    0, [self.summary(i) for i in self.installed]
                )
            case api.messages.GetMyStickersRequest():
                owned = [self.summary(i) for i, p in self.packs.items() if p.creator]
                sets = [
                    tl.StickerSetNoCovered(p) for p in owned if p.id > req.offset_id
                ][: req.limit]
                return tl.messages.MyStickers(len(owned), sets)
            case api.messages.GetStickerSetRequest():
                return self.details(self.set_id(req.stickerset))
            case api.messages.GetFavedStickersRequest():
                docs = [self.docs[i] for i in self.favorites]
                return tl.messages.FavedStickers(0, self.emoji(docs), docs)
            case api.messages.GetRecentStickersRequest():
                docs = [self.docs[i] for i in self.recent]
                return tl.messages.RecentStickers(
                    0, self.emoji(docs), docs, [NOW] * len(docs)
                )
            case _:
                raise NotImplementedError(type(req).__name__)

    async def download_media(self, document, file, thumb=None):
        self.counts["thumbnail" if thumb else "original"] += 1
        await asyncio.sleep(self.delay)
        file.write((self.thumbs if thumb else self.full)[document.id])
        return file

    async def upload_file(self, data, file_name):
        self.counts["upload_file"] += 1
        file_id = self.new_id()
        self.uploaded[file_id] = data
        return tl.InputFile(file_id, 1, file_name, "")

    async def send_once(self, req):
        name = type(req).__name__
        self.counts[name] += 1
        await asyncio.sleep(self.delay)
        if self.fail_at == name:
            self.fail_offset -= 1
            if not self.fail_offset:
                self.fail_at = None
                return False
        result = self.apply(req)
        if name == self.land_timeout:
            self.land_timeout = None
            raise TimeoutError
        return result

    def apply(self, req):
        match req:
            case api.messages.UploadMediaRequest():
                raw = self.uploaded.pop(req.media.file.id)
                return tl.MessageMediaDocument(
                    document=self.document(self.new_id(), raw)
                )
            case api.stickers.CreateStickerSetRequest():
                pack_id = self.new_id()
                self.packs[pack_id] = tl.StickerSet(
                    pack_id, 1, req.title, req.short_name, 0, 0, creator=True
                )
                self.grouped[pack_id] = []
                for item in req.stickers:
                    self.append(pack_id, item)
                return self.details(pack_id)
            case api.stickers.AddStickerToSetRequest():
                pack_id = self.set_id(req.stickerset)
                self.append(pack_id, req.sticker)
                return self.details(pack_id)
            case api.messages.InstallStickerSetRequest():
                self.installed.append(self.set_id(req.stickerset))
                return tl.messages.StickerSetInstallResultSuccess()
            case api.messages.ToggleStickerSetsRequest():
                for ref in req.stickersets:
                    if ref.id in self.installed:
                        self.installed.remove(ref.id)
                return True
            case api.messages.ReorderStickerSetsRequest():
                self.installed = req.order.copy()
                return True
            case (
                api.stickers.ChangeStickerPositionRequest()
                | api.stickers.RemoveStickerFromSetRequest()
            ):
                pack_id = sticker_attr(self.docs[req.sticker.id]).stickerset.id
                order = self.grouped[pack_id]
                order.remove(req.sticker.id)
                if isinstance(req, api.stickers.ChangeStickerPositionRequest):
                    order.insert(req.position, req.sticker.id)
                return self.details(pack_id)
            case api.stickers.RenameStickerSetRequest():
                pack_id = self.set_id(req.stickerset)
                self.packs[pack_id].title = req.title
                return self.details(pack_id)
            case api.messages.FaveStickerRequest():
                if req.id.id in self.favorites:
                    self.favorites.remove(req.id.id)
                if not req.unfave:
                    self.favorites.append(req.id.id)
                return True
            case api.messages.SaveRecentStickerRequest():
                if req.unsave and req.id.id in self.recent:
                    self.recent.remove(req.id.id)
                return True
            case _:
                raise NotImplementedError(type(req).__name__)

    def append(self, pack_id, item):
        # Telegram may assign a different ID when attaching the uploaded document.
        doc_id = self.new_id()
        self.document(doc_id, self.full[item.document.id], pack_id, emoji=item.emoji)
        self.grouped[pack_id].append(doc_id)

    def state(self):
        return {
            "calls": self.counts,
            "installed": [str(i) for i in self.installed],
            "packs": {
                p.title: [str(i) for i in self.grouped[p.id]]
                for p in self.packs.values()
            },
            "pack_ids": {p.title: str(p.id) for p in self.packs.values()},
            "favorites": [str(i) for i in self.favorites],
            "recent": [str(i) for i in self.recent],
        }


def fixture_app(fake=None):
    app = create_app()
    app.before_serving_funcs.clear()
    app.after_serving_funcs.clear()
    if fake is None:
        fake = FakeTelegram()
    queue = TelegramWriteQueue()
    media = MediaManager(fake)
    workspace = StickerWorkspace(fake, media, queue)
    app.tg_adapter = SimpleNamespace(
        stickers=workspace,
        is_connected=queue.is_running,
        _retired=False,
        account_token=workspace.token,
        display_name="Fixture User",
        profile_avatar_id=None,
    )
    app.chat_interactor = SimpleNamespace(_ensure_repository_current=lambda _: None)

    async def user(_):
        return None

    app.user_repo = SimpleNamespace(get_user=user)
    app.before_serving(queue.start)

    @app.after_serving
    async def cleanup():
        workspace.retire()
        await workspace.close()
        await queue.stop()
        await media.close()

    @app.route("/fixture", methods=["GET", "POST"])
    async def fixture_state():
        if request.method == "POST":
            body = await request.get_json()
            for name in ("fail_at", "fail_offset", "land_timeout", "delay"):
                if name in body:
                    setattr(fake, name, body[name])
            if body.get("reset_counts"):
                fake.counts.clear()
        return jsonify(fake.state())

    return app


if __name__ == "__main__":
    fixture_app().run(host="127.0.0.1", port=14873, use_reloader=False)
