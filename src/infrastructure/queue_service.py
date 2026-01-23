from typing import Optional
from arq import create_pool
from arq.connections import ArqRedis
from src.infrastructure.arq_config import get_redis_settings


class QueueService:
    def __init__(self):
        self._pool: ArqRedis | None = None

    async def connect(self):
        if not self._pool:
            self._pool = await create_pool(get_redis_settings())

    async def close(self):
        if self._pool:
            await self._pool.close()

    async def enqueue_mark_read(
        self, chat_id: int, topic_id: Optional[int] = None, max_id: Optional[int] = None
    ):
        if not self._pool:
            await self.connect()

        # Deduplication: Use a predictable job ID
        # If a read is already pending for this chat/topic, we don't need another one.
        job_id = f"read_{chat_id}_{topic_id}"

        await self._pool.enqueue_job(
            "mark_as_read_job",
            chat_id,
            topic_id,
            max_id,
            _job_id=job_id,
            _defer_by=0.5,  # Debounce slightly to allow bursts to settle
        )

    async def enqueue_reaction(self, chat_id: int, msg_id: int, emoji: str):
        if not self._pool:
            await self.connect()

        # Deduplication: One reaction per message per emoji
        job_id = f"react_{chat_id}_{msg_id}_{emoji}"

        await self._pool.enqueue_job(
            "send_reaction_job", chat_id, msg_id, emoji, _job_id=job_id
        )