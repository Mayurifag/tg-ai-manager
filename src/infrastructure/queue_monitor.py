import json
import time
from typing import Any, Dict, List

from redis.asyncio import Redis

from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class QueueMonitor:
    def __init__(self, redis_url: str):
        self.redis = Redis.from_url(redis_url, decode_responses=True)
        self.dead_letter_key = "queue:dead_letters"
        self.retention_seconds = 3 * 24 * 3600  # 3 days

    async def log_dead_letter(self, job_id: str, function: str, args: Any, error: str):
        """Logs a permanently failed job."""
        timestamp = time.time()
        payload = {
            "job_id": job_id,
            "function": function,
            "args": args,
            "error": error,
            "timestamp": timestamp,
        }

        try:
            # Add to a Sorted Set to allow time-based pruning
            # Score = timestamp
            data = json.dumps(payload)
            await self.redis.zadd(self.dead_letter_key, {data: timestamp})
            logger.error(
                "job_dead_letter", job_id=job_id, function=function, error=error
            )

            # Prune old logs immediately (lazy cleanup)
            cutoff = timestamp - self.retention_seconds
            await self.redis.zremrangebyscore(self.dead_letter_key, "-inf", cutoff)

        except Exception as e:
            logger.error("failed_to_log_dead_letter", error=str(e))

    async def get_failed_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            items = await self.redis.zrevrange(self.dead_letter_key, 0, limit - 1)
            return [json.loads(i) for i in items]
        except Exception as e:
            logger.error("failed_to_fetch_dead_letters", error=str(e))
            return []
