import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from dataclasses import dataclass, field
from src.domain.models import ChatType
from src.adapters.telegram import TelethonAdapter
from src.application.interactors import ChatInteractor # Updated import

@dataclass
class MockDialog:
    id: int
    name: str
    unread_count: int
    type: ChatType
    entity: MagicMock = field(default_factory=MagicMock)

    # Internal Telethon flags, hidden from init
    is_user: bool = field(init=False, default=False)
    is_group: bool = field(init=False, default=False)
    is_channel: bool = field(init=False, default=False)

    def __post_init__(self):
        # Map clean ChatType to dirty Telethon flags
        if self.type == ChatType.USER:
            self.is_user = True
        elif self.type == ChatType.GROUP:
            self.is_group = True
        elif self.type == ChatType.CHANNEL:
            self.is_channel = True
        elif self.type == ChatType.FORUM:
            self.is_channel = True
            self.entity.forum = True

@pytest.mark.asyncio
async def test_end_to_end_mocked_mtproto():
    with patch("src.adapters.telegram.TelegramClient") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.connect = AsyncMock()
        mock_instance.is_user_authorized = AsyncMock(return_value=True)
        mock_instance.disconnect = AsyncMock()

        fake_data = [
            MockDialog(1, "User A", 2, type=ChatType.USER),
            MockDialog(2, "Group B", 0, type=ChatType.GROUP),
            MockDialog(3, "Forum C", 100, type=ChatType.FORUM)
        ]
        mock_instance.get_dialogs = AsyncMock(return_value=fake_data)

        async def mock_call(request):
            mock_topic = MagicMock()
            mock_topic.unread_count = 5
            response = MagicMock()
            response.topics = [mock_topic] * 3
            return response

        mock_instance.__call__ = AsyncMock(side_effect=mock_call)

        adapter = TelethonAdapter("sess", 123, "hash")
        interactor = ChatInteractor(adapter) # Updated class name

        await interactor.initialize()
        chats = await interactor.get_recent_chats(limit=5)
        await interactor.shutdown()

        mock_instance.connect.assert_called_once()
        mock_instance.get_dialogs.assert_called_once_with(limit=5)

        assert len(chats) == 3

        assert chats[0].name == "User A"
        assert chats[0].unread_count == 2
        assert chats[0].type == ChatType.USER

        forum_chat = chats[2]
        assert forum_chat.type == ChatType.FORUM
        assert forum_chat.unread_count == 15
        assert forum_chat.unread_topics_count == 3
