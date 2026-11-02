import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart
from telethon import TelegramClient, errors, types
from telethon.sessions import StringSession

from src.adapters.telegram.media import MediaBusy, MediaManager, MediaTooLarge
from src.application.message_views import group_messages_into_albums
from src.domain.models import MediaContent, Message
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


def download_bytes(data):
    def download(*args, file, **kwargs):
        if data is None:
            return None
        file.write(data)
        return file

    return download


def test_group_messages_into_albums_does_not_mutate_source_messages():
    first = make_message(2, grouped_id=10)
    second = make_message(1, text="caption", grouped_id=10)
    grouped = group_messages_into_albums([first, second])
    assert first.text == ""
    assert first.album_parts is None
    assert grouped[0] is not first
    assert grouped[0].text == "caption"
    assert [part.id for part in grouped[0].album_parts] == [1, 2]


def test_group_messages_into_albums_keeps_interleaved_messages():
    newest = make_message(103, grouped_id=10)
    between = make_message(102, text="separate message")
    oldest = make_message(101, text="album caption", grouped_id=10)

    grouped = group_messages_into_albums([newest, between, oldest])

    assert [message.id for message in grouped] == [103, 102]
    assert [part.id for part in grouped[0].album_parts] == [101, 103]
    assert grouped[0].text == "album caption"
    assert grouped[1] is between


async def test_preloaded_photo_is_in_memory_and_reused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    raw = SimpleNamespace(id=12, media=types.MessageMediaPhoto())
    client = MagicMock()
    client.download_media = AsyncMock(side_effect=download_bytes(b"image"))
    manager = MediaManager(client)
    manager.prefetch_previews(42, [raw], [])
    await manager._preview_queue.join()
    result = await manager.download_media(42, 12)
    assert result == MediaContent(data=b"image", mime_type="image/jpeg")
    assert client.download_media.await_args.args == (raw,)
    assert client.download_media.await_args.kwargs["thumb"] == "m"
    client.get_entity.assert_not_called()
    client.get_messages.assert_not_called()
    assert list(tmp_path.iterdir()) == []
    await manager.close()


async def test_avatar_download_reuses_the_entity_returned_by_telegram():
    entity = types.User(
        id=42,
        access_hash=1,
        min=True,
        photo=types.UserProfilePhoto(photo_id=5, dc_id=1),
    )
    client = MagicMock()
    client.get_entity = AsyncMock(side_effect=ValueError("Entity not in cache"))
    client.download_profile_photo = AsyncMock(side_effect=download_bytes(b"avatar"))
    manager = MediaManager(client)
    assert await manager._get_chat_image(entity, 42) == "/media/avatar/42"
    assert await manager.get_chat_avatar(42) == b"avatar"
    client.get_entity.assert_not_awaited()
    assert client.download_profile_photo.await_args.args == (entity,)
    assert client.download_profile_photo.await_args.kwargs["download_big"] is False
    manager.clear_chat_avatar(42)
    assert 42 not in manager._avatar_entities
    await manager.close()


async def test_concurrent_photo_requests_share_download_when_one_is_cancelled():
    started, finish = asyncio.Event(), asyncio.Event()
    raw = SimpleNamespace(id=12, media=types.MessageMediaPhoto())
    client = MagicMock()

    async def download(*args, file, **kwargs):
        started.set()
        await finish.wait()
        file.write(b"image")
        return file

    client.download_media = AsyncMock(side_effect=download)
    manager = MediaManager(client)
    manager.remember_messages(42, [raw])
    first = asyncio.create_task(manager.download_media(42, 12))
    await started.wait()
    second = asyncio.create_task(manager.download_media(42, 12))
    await asyncio.sleep(0)
    first.cancel()
    finish.set()
    assert (await second).data == b"image"
    assert isinstance(
        (await asyncio.gather(first, return_exceptions=True))[0], asyncio.CancelledError
    )
    client.download_media.assert_awaited_once()
    await manager.close()


async def test_queued_image_is_available_to_foreground_without_lookup():
    three_started, finish = asyncio.Event(), asyncio.Event()
    count = 0

    async def download(message, *, file, **kwargs):
        nonlocal count
        count += 1
        if count >= 3:
            three_started.set()
        await finish.wait()
        file.write(b"image")
        return file

    client = MagicMock()
    client.download_media = AsyncMock(side_effect=download)
    manager = MediaManager(client)
    messages = [
        SimpleNamespace(id=i, media=types.MessageMediaPhoto()) for i in range(4)
    ]
    manager.prefetch_previews(42, messages, [])
    await asyncio.wait_for(three_started.wait(), timeout=2)
    foreground = asyncio.create_task(manager.download_media(42, 3))
    await asyncio.sleep(0)
    finish.set()
    assert (await foreground).data == b"image"
    await manager._preview_queue.join()
    client.get_entity.assert_not_called()
    client.get_messages.assert_not_called()
    assert client.download_media.await_count == 4
    await manager.close()


async def test_memory_cache_is_bounded_and_cleared_on_close(monkeypatch):
    monkeypatch.setenv("CACHE_MAX_SIZE_MB", "1")
    manager = MediaManager(None)
    manager._put(("one",), MediaContent(data=b"1" * 600_000, mime_type="image/jpeg"))
    manager._put(("two",), MediaContent(data=b"2" * 600_000, mime_type="image/jpeg"))
    assert manager._get(("one",)) is None
    assert manager._get(("two",)) is not None
    assert manager._cache_size <= 1024 * 1024
    await manager.close()
    assert not manager._cache
    assert manager._cache_size == 0


async def test_edited_media_invalidates_inflight_and_cached_downloads():
    started, finish = asyncio.Event(), asyncio.Event()
    old = SimpleNamespace(id=12, media=types.MessageMediaPhoto(), version="old")
    new = SimpleNamespace(id=12, media=types.MessageMediaPhoto(), version="new")
    client = MagicMock()

    async def download(message, *, file, **kwargs):
        if message.version == "old":
            started.set()
            await finish.wait()
        file.write(message.version.encode())
        return file

    client.download_media = AsyncMock(side_effect=download)
    manager = MediaManager(client)
    manager.remember_messages(42, [old])
    first = asyncio.create_task(manager.download_media(42, 12))
    await started.wait()
    manager.invalidate_message(42, 12)
    manager.remember_messages(42, [new])
    assert (await manager.download_media(42, 12)).data == b"new"
    finish.set()
    assert (await first).data == b"old"
    assert (await manager.download_media(42, 12)).data == b"new"
    assert client.download_media.await_count == 2
    manager.invalidate_message(42, 12)
    assert manager._get(("media", 42, 12, "preview")) is None
    assert (42, 12) not in manager._messages
    await manager.close()


async def test_photo_preview_without_medium_size_never_downloads_full_image():
    raw = SimpleNamespace(id=12, media=types.MessageMediaPhoto())
    client = MagicMock()

    def download(message, *, file, thumb):
        return download_bytes(None if thumb == "m" else b"thumbnail")(
            message, file=file
        )

    client.download_media = AsyncMock(side_effect=download)
    manager = MediaManager(client)
    manager.remember_messages(42, [raw])
    assert (await manager.download_media(42, 12)).data == b"thumbnail"
    assert [call.kwargs["thumb"] for call in client.download_media.await_args_list] == [
        "m",
        "x",
    ]
    await manager.close()


async def test_oversized_photo_is_rejected_before_full_download(monkeypatch):
    monkeypatch.setattr("src.adapters.telegram.media.MAX_DOWNLOAD_BYTES", 10)
    raw = SimpleNamespace(id=12, media=types.MessageMediaPhoto())
    client = MagicMock()
    calls = []

    async def download(message, *, file, **kwargs):
        file.write(b"123456")
        calls.append("first")
        file.write(b"78901")
        calls.append("second")
        return file

    client.download_media = AsyncMock(side_effect=download)
    manager = MediaManager(client)
    manager.remember_messages(42, [raw])
    try:
        with pytest.raises(MediaTooLarge):
            await manager.download_media(42, 12, size_type="full")
        assert calls == ["first"]
        assert not manager._cache
    finally:
        await manager.close()


async def test_inflight_downloads_are_bounded_and_existing_requests_share(monkeypatch):
    monkeypatch.setattr("src.adapters.telegram.media.MAX_ACTIVE_DOWNLOADS", 1)
    started, release = asyncio.Event(), asyncio.Event()
    client = MagicMock()

    async def download(message, *, file, **kwargs):
        started.set()
        await release.wait()
        file.write(b"image")
        return file

    client.download_media = AsyncMock(side_effect=download)
    manager = MediaManager(client)
    manager.remember_messages(
        42,
        [
            SimpleNamespace(id=12, media=types.MessageMediaPhoto()),
            SimpleNamespace(id=13, media=types.MessageMediaPhoto()),
        ],
    )
    first = asyncio.create_task(manager.download_media(42, 12))
    try:
        await asyncio.wait_for(started.wait(), 1)
        with pytest.raises(MediaBusy):
            await manager.download_media(42, 13)
        shared = asyncio.create_task(manager.download_media(42, 12))
        release.set()
        assert (await first).data == (await shared).data == b"image"
        assert client.download_media.await_count == 1
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await manager.close()


async def test_document_stream_refreshes_expired_reference_after_partial_data():
    old = SimpleNamespace(id=99, size=6)
    fresh = SimpleNamespace(id=99, size=6)
    message = SimpleNamespace(id=12, media=types.MessageMediaDocument(document=fresh))

    class Download:
        def __init__(self, chunks, expire=False):
            self.chunks = iter(chunks)
            self.expire = expire
            self._sender = True
            self.close = AsyncMock()

        def __aiter__(self):
            return self

        async def __anext__(self):
            chunk = next(self.chunks, None)
            if chunk is not None:
                return chunk
            if self.expire:
                raise errors.FileReferenceExpiredError(None)
            raise StopAsyncIteration

    first = Download([b"bc"], expire=True)
    second = Download([b"def"])
    client = MagicMock()
    client.iter_download.side_effect = [first, second]
    client.get_entity = AsyncMock(return_value="chat")
    client.get_messages = AsyncMock(return_value=[message])
    manager = MediaManager(client)
    chunks = [chunk async for chunk in manager.iter_document(42, 12, old, 1, 4)]
    assert b"".join(chunks) == b"bcde"
    assert [call.kwargs["offset"] for call in client.iter_download.call_args_list] == [
        1,
        3,
    ]
    assert client.iter_download.call_args_list[1].args[0] is fresh
    first.close.assert_awaited_once()
    second.close.assert_awaited_once()
    client.get_messages.assert_awaited_once_with("chat", ids=[12])
    await manager.close()


async def test_document_stream_uses_telethon_for_unaligned_ranges():
    payload = bytes(range(256)) * 5000
    document = types.Document(
        id=99,
        access_hash=1,
        file_reference=b"reference",
        date=None,
        mime_type="video/mp4",
        size=len(payload),
        dc_id=0,
        attributes=[],
    )
    client = TelegramClient(StringSession(), 12345, "0" * 32)

    async def telegram_response(sender, request):
        return SimpleNamespace(
            bytes=payload[request.offset : request.offset + request.limit]
        )

    client._call = AsyncMock(side_effect=telegram_response)
    manager = MediaManager(client)
    start, length = 500_003, 300_001
    chunks = [
        chunk async for chunk in manager.iter_document(42, 12, document, start, length)
    ]
    assert b"".join(chunks) == payload[start : start + length]
    assert client._call.await_count >= 2
    await manager.close()


async def test_document_stream_slot_is_released_even_before_first_chunk(monkeypatch):
    monkeypatch.setattr("src.adapters.telegram.media.MAX_ACTIVE_STREAMS", 1)
    manager = MediaManager(MagicMock())
    document = SimpleNamespace(size=10)
    first = manager.open_document_stream(42, 12, document, 0, 10)
    with pytest.raises(MediaBusy):
        manager.open_document_stream(42, 13, document, 0, 10)
    await first.aclose()
    second = manager.open_document_stream(42, 13, document, 0, 10)
    await second.aclose()
    assert not manager._stream_leases
    await manager.close()


async def test_manager_close_cancels_active_stream(monkeypatch):
    monkeypatch.setattr("src.adapters.telegram.media.MAX_ACTIVE_STREAMS", 1)
    started = asyncio.Event()

    class BlockingDownload:
        _sender = True

        def __init__(self):
            self.close = AsyncMock()

        def __aiter__(self):
            return self

        async def __anext__(self):
            started.set()
            await asyncio.Event().wait()

    download = BlockingDownload()
    client = MagicMock()
    client.iter_download.return_value = download
    manager = MediaManager(client)
    stream = manager.open_document_stream(42, 12, SimpleNamespace(size=10), 0, 10)
    consumer = asyncio.create_task(anext(stream))
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(manager.close(), 1)
    assert consumer.cancelled()
    download.close.assert_awaited_once()
    assert not manager._stream_leases


async def test_stream_overload_returns_503_before_sending_a_response(monkeypatch):
    monkeypatch.setattr("src.adapters.telegram.media.MAX_ACTIVE_STREAMS", 1)
    document = SimpleNamespace(size=10, mime_type="video/mp4", attributes=[])
    manager = MediaManager(MagicMock())
    held = manager.open_document_stream(42, 12, document, 0, 10)
    app = Quart(__name__)
    app.tg_adapter = SimpleNamespace(
        get_stream_document=AsyncMock(return_value=document),
        stream_document=manager.open_document_stream,
    )
    app.register_blueprint(media_routes.media_bp)
    try:
        response = await app.test_client().get("/media/stream/42/13")
        assert response.status_code == 503
        assert "busy" in (await response.get_json())["error"]
    finally:
        await held.aclose()
        await manager.close()


async def test_route_streams_actual_media_manager_lease_and_releases_it():
    document = SimpleNamespace(size=6, mime_type="video/mp4", attributes=[])

    class Download:
        _sender = True

        def __init__(self, offset):
            self.chunks = iter([b"abcdef"[offset:]])
            self.close = AsyncMock()

        def __aiter__(self):
            return self

        async def __anext__(self):
            chunk = next(self.chunks, None)
            if chunk is None:
                raise StopAsyncIteration
            return chunk

    client = MagicMock()
    client.iter_download.side_effect = lambda document, offset, **kwargs: Download(
        offset
    )
    manager = MediaManager(client)
    app = Quart(__name__)
    app.tg_adapter = SimpleNamespace(
        get_stream_document=AsyncMock(return_value=document),
        stream_document=manager.open_document_stream,
    )
    app.register_blueprint(media_routes.media_bp)
    try:
        response = await app.test_client().get(
            "/media/stream/42/12", headers={"Range": "bytes=1-3"}
        )
        assert response.status_code == 206
        assert response.headers["Content-Range"] == "bytes 1-3/6"
        assert await response.get_data() == b"bcd"
        assert not manager._stream_leases
    finally:
        await manager.close()


async def test_media_routes_use_ram_and_stream_ranges_without_caching():
    document = SimpleNamespace(size=6, mime_type="video/mp4", attributes=[])

    async def chunks(chat_id, message_id, document, start, length):
        yield b"abcdef"[start : start + length]

    app = Quart(__name__)
    app.chat_interactor = SimpleNamespace(
        get_media=AsyncMock(
            return_value=MediaContent(data=b"photo", mime_type="image/jpeg")
        ),
        get_chat_avatar=AsyncMock(return_value=b"avatar"),
    )
    app.tg_adapter = SimpleNamespace(
        get_stream_document=AsyncMock(return_value=document), stream_document=chunks
    )
    app.register_blueprint(media_routes.media_bp)
    client = app.test_client()
    response = await client.get("/media/42/1")
    assert await response.get_data() == b"photo"
    assert response.headers["Cache-Control"] == "no-store"
    avatar = await client.get("/media/avatar/42")
    assert await avatar.get_data() == b"avatar"
    assert avatar.headers["Cache-Control"] == "no-store"
    response = await client.get("/media/stream/42/1", headers={"Range": "bytes=1-3"})
    assert response.status_code == 206
    assert response.headers["Content-Range"] == "bytes 1-3/6"
    assert await response.get_data() == b"bcd"
    response = await client.get("/media/stream/42/1", headers={"Range": "bytes=99-100"})
    assert response.status_code == 416
    assert response.headers["Content-Range"] == "bytes */6"

    response = await client.get("/media/stream/42/1", headers={"Range": "bytes=-2"})
    assert response.status_code == 206
    assert await response.get_data() == b"ef"
    response = await client.get("/media/stream/42/1")
    assert response.status_code == 200
    assert await response.get_data() == b"abcdef"

    app.chat_interactor.get_media.return_value = MediaContent(
        data=b"<script>evil()</script>", mime_type="text/html"
    )
    response = await client.get("/media/42/1")
    assert response.mimetype == "application/octet-stream"
    assert response.headers["Content-Disposition"] == "attachment"

    document.mime_type = "application/pdf"
    document.attributes = [types.DocumentAttributeFilename("тест\r\n/файл.pdf")]
    response = await client.get("/media/stream/42/1")
    assert response.status_code == 200
    disposition = response.headers["Content-Disposition"]
    assert "filename*=UTF-8''%D1%82%D0%B5%D1%81%D1%82_" in disposition
    assert "\r" not in disposition and "\n" not in disposition
