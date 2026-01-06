import json
import time
from datetime import datetime
from typing import List, TypeVar, Generic, Dict, Any
from dataclasses import asdict, is_dataclass
from redis.asyncio import Redis
from src.domain.ports import ActionRepository, EventRepository
from src.domain.models import ActionLog, SystemEvent, Message
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class BaseValkeyLogRepository(Generic[T]):
    """
    Shared logic for logging time-series data to Valkey (Redis) using ZSETs.
    Handles serialization, insertion with scoring, and manual cleanup.
    """

    def __init__(self, redis_url: str, key_prefix: str, ttl_seconds: int):
        self.redis = Redis.from_url(redis_url, decode_responses=True)
        self.key_prefix = key_prefix
        self.ttl_seconds = ttl_seconds

    def _serialize(self, obj: Any) -> Any:
        """Recursively convert datetime objects to ISO format."""
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {k: self._serialize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._serialize(v) for v in obj]
        if is_dataclass(obj) and not isinstance(obj, type):
            return self._serialize(asdict(obj))
        return obj

    async def _add_item(self, item_dict: Dict[str, Any], score: float) -> None:
        try:
            # 1. Serialize
            data_str = json.dumps(self._serialize(item_dict))

            # 2. Add to Sorted Set
            await self.redis.zadd(self.key_prefix, {data_str: score})

        except Exception as e:
            logger.error(f"{self.key_prefix}_add_failed", error=str(e))

    async def cleanup_expired(self) -> None:
        """Removes items older than ttl_seconds."""
        try:
            now = time.time()
            cutoff = now - self.ttl_seconds
            removed = await self.redis.zremrangebyscore(self.key_prefix, "-inf", cutoff)
            if removed > 0:
                logger.info(f"{self.key_prefix}_cleanup", removed_count=removed)
        except Exception as e:
            logger.error(f"{self.key_prefix}_cleanup_failed", error=str(e))

    async def _fetch_items(self, limit: int) -> List[Dict[str, Any]]:
        try:
            # ZREVRANGE: Newest (highest score) first
            items = await self.redis.zrevrange(self.key_prefix, 0, limit - 1)
            return [json.loads(i) for i in items]
        except Exception as e:
            logger.error(f"{self.key_prefix}_fetch_failed", error=str(e))
            return []


class ValkeyActionRepository(BaseValkeyLogRepository[ActionLog], ActionRepository):
    def __init__(self, redis_url: str):
        # 3 Hours TTL (10800 seconds)
        super().__init__(redis_url, key_prefix="action_log", ttl_seconds=10800)
        self.sequence_key = "action_log_seq"

    async def add_log(self, log: ActionLog) -> None:
        try:
            # Generate ID
            log_id = await self.redis.incr(self.sequence_key)
            log.id = log_id

            # Convert to dict for storage
            log_dict = asdict(log)

            await self._add_item(log_dict, log.date.timestamp())
        except Exception as e:
            logger.error("action_log_add_wrapper_failed", error=str(e))

    async def get_logs(self, limit: int = 50) -> List[ActionLog]:
        dicts = await self._fetch_items(limit)
        results = []
        for d in dicts:
            # Reconstruct datetime
            if "date" in d and isinstance(d["date"], str):
                d["date"] = datetime.fromisoformat(d["date"])
            results.append(ActionLog(**d))
        return results


class ValkeyEventRepository(BaseValkeyLogRepository[SystemEvent], EventRepository):
    def __init__(self, redis_url: str):
        # 3 Hours TTL (10800 seconds)
        super().__init__(redis_url, key_prefix="system_events", ttl_seconds=10800)

    async def add_event(self, event: SystemEvent) -> None:
        # We don't need to persist 'rendered_html' as it is transient/large
        event_copy = asdict(event)
        event_copy["rendered_html"] = None

        await self._add_item(event_copy, event.date.timestamp())

    async def get_recent_events(self, limit: int = 10) -> List[SystemEvent]:
        dicts = await self._fetch_items(limit)
        results = []
        for d in dicts:
            # Recursively reconstruct datatypes
            if "date" in d and isinstance(d["date"], str):
                d["date"] = datetime.fromisoformat(d["date"])

            # Reconstruct nested Message object if present
            if d.get("message_model"):
                msg_data = d["message_model"]
                if "date" in msg_data and isinstance(msg_data["date"], str):
                    msg_data["date"] = datetime.fromisoformat(msg_data["date"])
                d["message_model"] = Message(**msg_data)

            results.append(SystemEvent(**d))
        return results
