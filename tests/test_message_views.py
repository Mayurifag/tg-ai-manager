from datetime import datetime
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telethon import types
from quart import Quart
from src.adapters.telegram.media import MediaManager
from src.application.message_views import group_messages_into_albums
from src.domain.models import Message
from src.web.routes import media as media_routes


def make_message(id: int, text: str = "", grouped_id: int | None = None) -> Message:
    return Message(
        id=id,
        text=text,
        date=datetime.now(),
        sender_name="Sender",
        is_outgoing=False,
        grouped_id=grouped_id,
    )


def test_group_messages_into_albums_does_not_mutate_source_messages():
    first = make_message(2, grouped_id=10)
    second = make_message(1, text="caption", grouped_id=10)

    grouped = group_messages_into_albums([first, second])

    assert first.text == ""
    assert first.album_parts is None
    assert grouped[0] is not first
    assert grouped[0].text == "caption"
    assert [part.id for part in grouped[0].album_parts] == [1, 2]


async def test_cached_media_does_not_fetch_telegram_again(tmp_path):
    (tmp_path / "media_-100_1.jpg").write_bytes(b"cached")
    client = MagicMock()
    manager = MediaManager(client, str(tmp_path))
    assert await manager.download_media(-100, 1) == "/cache/media_-100_1.jpg"
    client.get_entity.assert_not_called()


async def test_concurrent_media_requests_share_a_download(tmp_path):
    manager = MediaManager(None, str(tmp_path))
    started, finish = asyncio.Event(), asyncio.Event()

    async def download(*args):
        started.set()
        await finish.wait()
        return "/cache/photo.jpg"

    manager._download_media = AsyncMock(side_effect=download)
    first = asyncio.create_task(manager.download_media(1, 2))
    await started.wait()
    second = asyncio.create_task(manager.download_media(1, 2))
    await asyncio.sleep(0)
    first.cancel()
    finish.set()
    assert await second == "/cache/photo.jpg"
    assert (await asyncio.gather(first, return_exceptions=True))[
        0
    ].__class__ is asyncio.CancelledError
    manager._download_media.assert_awaited_once()


async def test_preload_uses_fetched_message_and_never_serves_partial_file(tmp_path):
    started, finish = asyncio.Event(), asyncio.Event()
    raw = SimpleNamespace(id=12, media=types.MessageMediaPhoto())
    client = MagicMock()

    async def download(message, *, file, **kwargs):
        assert message is raw
        with open(file, "wb") as output:
            output.write(b"partial")
        started.set()
        await finish.wait()
        with open(file, "ab") as output:
            output.write(b" complete")
        return file

    client.download_media = AsyncMock(side_effect=download)
    manager = MediaManager(client, str(tmp_path))
    manager.prefetch_previews(42, [raw], [])
    await started.wait()
    assert list(tmp_path.glob("*.part"))
    assert not (tmp_path / "media_42_12.jpg").exists()
    finish.set()
    await manager._preview_queue.join()
    assert (tmp_path / "media_42_12.jpg").read_bytes() == b"partial complete"
    assert await manager.download_media(42, 12) == "/cache/media_42_12.jpg"
    client.get_entity.assert_not_called()
    client.get_messages.assert_not_called()
    await manager.close()


async def test_queued_image_is_available_to_foreground_without_lookup(tmp_path):
    three_started, finish = asyncio.Event(), asyncio.Event()
    count = 0

    async def download(message, *, file, **kwargs):
        nonlocal count
        count += 1
        if count >= 3:
            three_started.set()
        await finish.wait()
        with open(file, "wb") as output:
            output.write(b"image")
        return file

    client = MagicMock()
    client.download_media = AsyncMock(side_effect=download)
    manager = MediaManager(client, str(tmp_path))
    messages = [
        SimpleNamespace(id=i, media=types.MessageMediaPhoto()) for i in range(4)
    ]
    manager.prefetch_previews(42, messages, [])
    await asyncio.wait_for(three_started.wait(), timeout=2)
    foreground = asyncio.create_task(manager.download_media(42, 3))
    await asyncio.sleep(0)
    finish.set()
    assert await foreground == "/cache/media_42_3.jpg"
    await manager._preview_queue.join()
    client.get_entity.assert_not_called()
    client.get_messages.assert_not_called()
    assert client.download_media.await_count == 4
    await manager.close()


async def test_persisted_media_is_scoped_to_the_active_account(tmp_path, monkeypatch):
    manager = MediaManager(None, str(tmp_path))
    manager.set_account(123)
    (tmp_path / "123" / "media_42_1.jpg").write_bytes(b"account 123")
    assert await manager.download_media(42, 1) == "/cache/123/media_42_1.jpg"
    manager.set_account(456)
    (tmp_path / "456" / "media_42_1.jpg").write_bytes(b"account 456")
    assert await manager.download_media(42, 1) == "/cache/456/media_42_1.jpg"

    monkeypatch.setattr(media_routes, "IMAGES_DIR", str(tmp_path))
    app = Quart(__name__)
    app.tg_adapter = SimpleNamespace(images_dir=manager.images_dir)
    app.register_blueprint(media_routes.media_bp)
    client = app.test_client()
    assert (await client.get("/cache/123/media_42_1.jpg")).status_code == 404
    response = await client.get("/cache/456/media_42_1.jpg")
    assert response.status_code == 200
    assert await response.get_data() == b"account 456"
