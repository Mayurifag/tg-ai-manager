from typing import Protocol, Any, Awaitable
from telethon import TelegramClient, functions
from src.domain.ports import ChatRepository
from src.domain.models import Chat, ChatType

# Protocol to enforce correct async signatures for the external library
class ITelethonClient(Protocol):
    def connect(self) -> Awaitable[None]: ...
    def disconnect(self) -> Awaitable[None]: ...
    def is_user_authorized(self) -> Awaitable[bool]: ...
    def start(self) -> Awaitable[Any]: ...
    def get_dialogs(self, limit: int) -> Awaitable[Any]: ...
    def __call__(self, request: Any) -> Awaitable[Any]: ...

class TelethonAdapter(ChatRepository):
    def __init__(self, session_name: str, api_id: int, api_hash: str):
        # We cast the client to our Protocol once here.
        # This isolates the type correction to a single line.
        self.client: ITelethonClient = TelegramClient(session_name, api_id, api_hash) # type: ignore

    async def connect(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.start()

    async def disconnect(self):
        await self.client.disconnect()

    async def get_chats(self, limit: int) -> list[Chat]:
        dialogs = await self.client.get_dialogs(limit=limit)
        results = []
        for d in dialogs:
            chat_type = ChatType.GROUP  # Default fallback for groups/megagroups

            if d.is_user:
                chat_type = ChatType.USER
            elif d.is_channel:
                if getattr(d.entity, 'forum', False):
                    chat_type = ChatType.FORUM
                elif not d.is_group:
                    chat_type = ChatType.CHANNEL
                # Else: it's a channel + group => Megagroup => ChatType.GROUP

            unread_topics_count = None
            calculated_unread_count = d.unread_count

            # Check if it's a forum to fetch topics
            if chat_type == ChatType.FORUM:
                try:
                    # Fetch active topics to get detailed unread info
                    response = await self.client(functions.messages.GetForumTopicsRequest(
                        peer=d.entity,
                        offset_date=None,
                        offset_id=0,
                        offset_topic=0,
                        limit=20,
                        q=''
                    ))

                    active_topics = response.topics
                    unread_topics = [t for t in active_topics if t.unread_count > 0]

                    if unread_topics:
                        unread_topics_count = len(unread_topics)
                        # Recalculate unread count based on active topics to match "reality"
                        calculated_unread_count = sum(t.unread_count for t in unread_topics)

                except Exception as e:
                    print(f"Failed to fetch topics for forum {d.name}: {e}")

            results.append(Chat(
                id=d.id,
                name=d.name,
                unread_count=calculated_unread_count,
                type=chat_type,
                unread_topics_count=unread_topics_count
            ))
        return results
