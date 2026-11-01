from datetime import datetime
import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.adapters.telegram.media import MediaManager
from src.application.message_views import group_messages_into_albums
from src.domain.models import Message


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
