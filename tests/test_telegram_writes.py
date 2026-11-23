import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import errors, types

from src.adapters.telegram.chat_query_ops import ChatQueryOps
from src.adapters.telegram.event_handlers import EventHandlers
from src.adapters.telegram.message_parser import MessageParser
from src.adapters.telegram.read_ops import ReadOps
from src.adapters.telegram.write_ops import WriteOps
from src.infrastructure.telegram_queue import TelegramWriteQueue


class TelegramWrites(unittest.IsolatedAsyncioTestCase):
    async def test_definite_reaction_rejection_does_not_block_other_messages(self):
        client = SimpleNamespace(
            get_input_entity=AsyncMock(return_value=types.InputPeerUser(42, 1)),
            get_messages=AsyncMock(return_value=[SimpleNamespace(reactions=None)]),
            send_once=AsyncMock(side_effect=errors.ReactionInvalidError(None)),
        )
        ops = WriteOps(client, None, None, None)
        self.assertIs(await ops._send_reaction(42, 1, "👍"), False)
        self.assertIsNone(ops._reaction_blocked(42, 1))
        client.send_once.side_effect = TimeoutError
        self.assertIsNone(await ops._send_reaction(42, 1, "👍"))
        self.assertIsNotNone(ops._reaction_blocked(42, 1))
        self.assertIsNone(ops._reaction_blocked(42, 2))

    async def test_manual_read_retries_same_bound_without_blocking_other_work(self):
        first_write, finished, unrelated = (asyncio.Event() for _ in range(3))
        writes = []

        class Client:
            get_input_entity = AsyncMock(return_value=types.InputPeerUser(42, 1))

            async def __call__(self, request):
                self.lookups += 1
                return SimpleNamespace(
                    dialogs=[
                        SimpleNamespace(
                            peer=types.PeerUser(42),
                            read_inbox_max_id=0,
                            top_message=9 if self.lookups == 1 else 12,
                            unread_count=1,
                        )
                    ]
                )

            lookups = 0

            async def send_read_acknowledge(self, peer, *, max_id):
                writes.append(max_id)
                if len(writes) == 1:
                    first_write.set()
                    raise errors.FloodWaitError(None, capture=1)
                return True

        async def notify(event):
            if event.type == "read":
                finished.set()

        async def other_write():
            unrelated.set()

        client, queue = Client(), TelegramWriteQueue()
        ops = ReadOps(client, queue, notify)
        await queue.start()
        try:
            async with asyncio.timeout(3):
                await ops.mark_as_read(42)
                await first_write.wait()
                await queue.enqueue(other_write)
                await unrelated.wait()
                self.assertEqual(writes, [9])
                await finished.wait()
            self.assertEqual(writes, [9, 9])
            self.assertEqual(client.lookups, 1)
        finally:
            await queue.stop()
            await ops.close()

    async def test_selective_read_rechecks_unread_state_after_flood_wait(self):
        for still_eligible in (True, False):
            with self.subTest(still_eligible=still_eligible):
                await self.check_selective_read_retry(still_eligible)

    async def check_selective_read_retry(self, still_eligible):
        limited, rechecked, unrelated, completed = (asyncio.Event() for _ in range(4))
        peer = types.PeerUser(42)

        class Client:
            get_input_entity = AsyncMock(return_value=types.InputPeerUser(42, 1))
            send_read_acknowledge = AsyncMock(return_value=True)

            async def __call__(self, request):
                if not limited.is_set():
                    limited.set()
                    raise errors.FloodWaitError(request, capture=1)
                rechecked.set()
                return SimpleNamespace(
                    dialogs=[
                        SimpleNamespace(
                            peer=peer,
                            read_inbox_max_id=0,
                            top_message=9 if still_eligible else 10,
                            unread_count=1 if still_eligible else 2,
                        )
                    ]
                )

        async def on_success():
            completed.set()

        async def other_write():
            unrelated.set()

        client, queue = Client(), TelegramWriteQueue()
        ops = ReadOps(client, queue, None)
        try:
            await queue.start()
            async with asyncio.timeout(3):
                await ops.mark_automatic_as_read(
                    42,
                    None,
                    9,
                    AsyncMock(return_value=True),
                    on_success,
                    selective=True,
                    chat_name="Fixture",
                    topic_name=None,
                )
                await limited.wait()
                await queue.enqueue(other_write)
                await unrelated.wait()
                self.assertFalse(rechecked.is_set())
                await rechecked.wait()
                await queue._queue.join()
                if still_eligible:
                    await completed.wait()
                    client.send_read_acknowledge.assert_awaited_once_with(
                        types.InputPeerUser(42, 1), max_id=9
                    )
                else:
                    client.send_read_acknowledge.assert_not_awaited()
                    self.assertFalse(completed.is_set())
        finally:
            await queue.stop()
            await ops.close()

    async def test_inaccessible_peer_does_not_abort_the_rest_of_the_batch(self):
        class Client:
            async def get_input_entity(self, peer):
                return types.InputPeerUser(peer, 1)

            async def __call__(self, request):
                peers = [entry.peer.user_id for entry in request.peers]
                if 1 in peers:
                    raise errors.ChannelPrivateError(request)
                return SimpleNamespace(
                    chats=[],
                    users=[types.User(2, first_name="Valid")],
                    dialogs=[
                        SimpleNamespace(
                            peer=types.PeerUser(2),
                            unread_count=1,
                            top_message=5,
                            read_inbox_max_id=0,
                        )
                    ],
                )

        ops = ChatQueryOps(Client(), None, None)
        self.assertEqual(
            [chat.id for chat in await ops.get_all_unread_chats([1, 2])], [2]
        )
        self.assertIn(1, ops._unavailable_chats)

    async def test_partial_reactions_use_authoritative_selection(self):
        emoji = types.ReactionEmoji("👍")
        full = types.MessageReactions([types.ReactionCount(emoji, 2, chosen_order=0)])
        partial = types.MessageReactions([types.ReactionCount(emoji, 2)], min=True)
        client = SimpleNamespace(
            get_messages=AsyncMock(return_value=[SimpleNamespace(reactions=full)])
        )
        handlers = EventHandlers(client, MessageParser(client, None), None, AsyncMock())
        receive = AsyncMock()
        handlers.add_event_listener(receive)
        update = types.UpdateMessageReactions(types.PeerUser(42), 7, partial)
        update._entities = {42: types.User(42, first_name="Fixture")}
        await handlers._process_reaction_update(update)
        client.get_messages.assert_awaited_once_with(42, ids=[7])
        self.assertTrue(receive.await_args.args[0].message_model.reactions[0].is_chosen)
