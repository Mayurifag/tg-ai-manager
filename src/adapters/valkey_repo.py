import json
import time
from datetime import datetime
from typing import List, TypeVar, Generic, Dict, Any, Optional
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
    Handles serialization, insertion with scoring, and time-based auto-expiration.
    """
    def __init__(self, redis_url: str, key_prefix: str, ttl_seconds: int):
        self.redis = Redis.from_url(redis_url, decode_responses=True)
        self.key_prefix = key_prefix
        self.ttl_seconds = ttl_seconds

        # Optimization: Track cleanup locally to avoid ZREMRANGEBYSCORE calls on every insert
        self._last_cleanup_time = 0.0
        # Cleanup at most once every 10% of TTL or 5 minutes, whichever is smaller
        self._cleanup_interval = min(300, max(60, ttl_seconds // 10))

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

            # 3. Conditional Cleanup
            now = time.time()
            if now - self._last_cleanup_time > self._cleanup_interval:
                await self._cleanup_old_items(now)

        except Exception as e:
            logger.error(f"{self.key_prefix}_add_failed", error=str(e))

    async def _cleanup_old_items(self, now: float) -> None:
        try:
            cutoff = now - self.ttl_seconds
            removed = await self.redis.zremrangebyscore(self.key_prefix, "-inf", cutoff)
            if removed > 0:
                logger.debug(f"{self.key_prefix}_cleanup", removed_count=removed)
            self._last_cleanup_time = now
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
        # 24 Hours (86400 seconds) TTL
        super().__init__(redis_url, key_prefix="action_log", ttl_seconds=86400)
        self.sequence_key = "action_log_seq"

    async def add_log(self, log: ActionLog) -> None:
        try:
            # Generate ID
            log_id = await self.redis.incr(self.sequence_key)
            log.id = log_id

            # Convert to dict for storage
            # We construct dict manually to ensure flattened structure matching previous impl if needed,
            # or just use asdict. Using asdict is cleaner.
            log_dict = asdict(log)

            await self._add_item(log_dict, log.date.timestamp())
        except Exception as e:
            logger.error("action_log_add_wrapper_failed", error=str(e))

    async def get_logs(self, limit: int = 50) -> List[ActionLog]:
        dicts = await self._fetch_items(limit)
        results = []
        for d in dicts:
            # Reconstruct datetime
            if 'date' in d and isinstance(d['date'], str):
                d['date'] = datetime.fromisoformat(d['date'])
            results.append(ActionLog(**d))
        return results

class ValkeyEventRepository(BaseValkeyLogRepository[SystemEvent], EventRepository):
    def __init__(self, redis_url: str):
        # 1 Hour (3600 seconds) TTL
        super().__init__(redis_url, key_prefix="system_events", ttl_seconds=3600)

    async def add_event(self, event: SystemEvent) -> None:
        # We don't need to persist 'rendered_html' as it is transient/large
        # and re-generated or unused in sidebar history.
        event_copy = asdict(event)
        event_copy['rendered_html'] = None

        await self._add_item(event_copy, event.date.timestamp())

    async def get_recent_events(self, limit: int = 10) -> List[SystemEvent]:
        dicts = await self._fetch_items(limit)
        results = []
        for d in dicts:
            # Recursively reconstruct datatypes
            if 'date' in d and isinstance(d['date'], str):
                d['date'] = datetime.fromisoformat(d['date'])

            # Reconstruct nested Message object if present
            if d.get('message_model'):
                msg_data = d['message_model']
                if 'date' in msg_data and isinstance(msg_data['date'], str):
                    msg_data['date'] = datetime.fromisoformat(msg_data['date'])
                d['message_model'] = Message(**msg_data)

            results.append(SystemEvent(**d))
        return results
