import asyncio
from types import SimpleNamespace

from telethon import functions

from src.adapters.telegram.read_ops import ReadOps


class ManualQueue:
    def __init__(self):
        self.items = []

    async def enqueue(self, coro_fn):
        self.items.append(coro_fn)

    async def run_next(self):
        await self.items.pop(0)()


class FakeClient:
    def __init__(self, messages=None):
        self.messages = messages or []
        self.requests = []
        self.get_messages_calls = []
        self.read_ack_calls = []

    async def get_input_entity(self, chat_id):
        return f"peer-{chat_id}"

    async def get_messages(self, entity, limit=1, reply_to=None):
        self.get_messages_calls.append((entity, limit, reply_to))
        return self.messages

    async def get_entity(self, input_peer):
        return SimpleNamespace(title=str(input_peer))

    async def send_read_acknowledge(self, entity, max_id=None):
        self.read_ack_calls.append((entity, max_id))

    async def __call__(self, request):
        self.requests.append(request)
        return True


async def get_topic_name(chat_id, topic_id):
    return f"Topic {topic_id}"


def make_ops(client, queue, dispatch=None, coalesce_delay=0):
    return ReadOps(
        client=client,
        write_queue=queue,
        dispatch_fn=dispatch,
        get_topic_name_fn=get_topic_name,
        coalesce_delay=coalesce_delay,
    )


async def run_enqueued_read(queue):
    await asyncio.sleep(0)
    assert len(queue.items) == 1
    await queue.run_next()


async def test_mark_topic_read_without_max_id_uses_latest_topic_message():
    queue = ManualQueue()
    client = FakeClient(messages=[SimpleNamespace(id=123)])
    ops = make_ops(client, queue)

    await ops.mark_as_read(100, topic_id=10)
    await run_enqueued_read(queue)

    assert client.get_messages_calls == [("peer-100", 1, 10)]
    assert len(client.requests) == 1
    request = client.requests[0]
    assert isinstance(request, functions.messages.ReadDiscussionRequest)
    assert request.msg_id == 10
    assert request.read_max_id == 123


async def test_mark_topic_read_with_max_id_does_not_fetch_latest_message():
    queue = ManualQueue()
    client = FakeClient(messages=[SimpleNamespace(id=123)])
    ops = make_ops(client, queue)

    await ops.mark_as_read(100, topic_id=10, max_id=55)
    await run_enqueued_read(queue)

    assert client.get_messages_calls == []
    request = client.requests[0]
    assert isinstance(request, functions.messages.ReadDiscussionRequest)
    assert request.msg_id == 10
    assert request.read_max_id == 55


async def test_mark_chat_read_debounces_pending_max_ids():
    events = []

    async def dispatch(event):
        events.append(event)

    queue = ManualQueue()
    client = FakeClient()
    ops = make_ops(client, queue, dispatch=dispatch, coalesce_delay=0.01)

    for msg_id in range(1, 11):
        await ops.mark_as_read(100, max_id=msg_id)

    await asyncio.sleep(0)
    assert queue.items == []

    await asyncio.sleep(0.02)
    assert len(queue.items) == 1
    await queue.run_next()

    assert client.read_ack_calls == [("peer-100", 10)]
    assert len(events) == 1


async def test_mark_topic_read_coalesces_pending_max_ids():
    queue = ManualQueue()
    client = FakeClient()
    ops = make_ops(client, queue)

    await ops.mark_as_read(100, topic_id=10, max_id=21)
    await ops.mark_as_read(100, topic_id=10, max_id=25)
    await ops.mark_as_read(100, topic_id=10, max_id=23)
    await run_enqueued_read(queue)

    assert client.get_messages_calls == []
    assert len(client.requests) == 1
    request = client.requests[0]
    assert isinstance(request, functions.messages.ReadDiscussionRequest)
    assert request.msg_id == 10
    assert request.read_max_id == 25


async def test_unbounded_mark_chat_read_wins_over_pending_max_id():
    queue = ManualQueue()
    client = FakeClient()
    ops = make_ops(client, queue)

    await ops.mark_as_read(100, max_id=10)
    await ops.mark_as_read(100)
    await run_enqueued_read(queue)

    assert client.read_ack_calls == [("peer-100", None)]


async def test_different_chats_keep_separate_read_all_calls():
    queue = ManualQueue()
    client = FakeClient()
    ops = make_ops(client, queue)

    await ops.mark_as_read(100)
    await ops.mark_as_read(200)
    await asyncio.sleep(0)

    assert len(queue.items) == 2

    await queue.run_next()
    await queue.run_next()

    assert client.read_ack_calls == [("peer-100", None), ("peer-200", None)]
