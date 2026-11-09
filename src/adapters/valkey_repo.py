import json
import time
from datetime import datetime
from typing import List, TypeVar, Generic, Dict, Any
from dataclasses import asdict, is_dataclass, replace
from redis.asyncio import Redis
from src.domain.errors import LogUnavailable
from src.domain.ports import ActionRepository, EventRepository
from src.domain.models import ActionLog, SystemEvent
from src.infrastructure.logging import get_logger
from src.infrastructure.tasks import raise_if_cancelled, raise_if_task_stopped

logger = get_logger(__name__)

T = TypeVar("T")
MAX_LOG_ENTRIES = 10_000
LOG_RETENTION_SECONDS = 3 * 60 * 60


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

    @staticmethod
    def _restore_date(record: Any) -> None:
        if not isinstance(record, dict) or not isinstance(record.get("date"), str):
            raise ValueError("Invalid history date")
        record["date"] = datetime.fromisoformat(record["date"])

    async def close(self) -> None:
        await self.redis.aclose()

    def _queue_retention(self, pipe: Any) -> None:
        pipe.zremrangebyscore(self.key_prefix, "-inf", time.time() - self.ttl_seconds)
        pipe.zremrangebyrank(self.key_prefix, 0, -MAX_LOG_ENTRIES - 1)
        # Last-write/cleanup expiry is a fallback if the hourly job stops.
        # Individual entries can survive up to roughly twice the TTL in that case.
        pipe.expire(self.key_prefix, self.ttl_seconds)

    async def _add_item(self, item_dict: Dict[str, Any], score: float) -> None:
        try:
            raise_if_task_stopped()
            data_str = json.dumps(self._serialize(item_dict))
            async with self.redis.pipeline(transaction=True) as pipe:
                raise_if_task_stopped()
                pipe.zadd(self.key_prefix, {data_str: score})
                self._queue_retention(pipe)
                await pipe.execute()
                raise_if_task_stopped()
        except Exception as error:
            logger.warning(
                f"{self.key_prefix}_add_failed", category=type(error).__name__
            )
            raise LogUnavailable("History storage unavailable") from error
        finally:
            raise_if_task_stopped()

    async def cleanup_expired(self) -> None:
        """Trim expired and excess records even if no new events arrive."""
        try:
            raise_if_cancelled()
            async with self.redis.pipeline(transaction=True) as pipe:
                raise_if_cancelled()
                self._queue_retention(pipe)
                expired, excess, _ = await pipe.execute()
                raise_if_cancelled()
            raise_if_cancelled()
            removed = expired + excess
            if removed > 0:
                logger.info(f"{self.key_prefix}_cleanup", removed_count=removed)
        except Exception as error:
            logger.warning(
                f"{self.key_prefix}_cleanup_failed", category=type(error).__name__
            )
        finally:
            raise_if_cancelled()

    async def _fetch_items(self, limit: int) -> List[Dict[str, Any]]:
        raise_if_cancelled()
        if limit <= 0:
            return []
        try:
            # ZREVRANGE: Newest (highest score) first
            # shortcut: one request returns at most 1,000 log records; revisit
            # if an explicit paginated history interface needs larger pages.
            items = await self.redis.zrevrange(self.key_prefix, 0, min(limit, 1000) - 1)
            raise_if_cancelled()
            return [json.loads(i) for i in items]
        except Exception as error:
            logger.warning(
                f"{self.key_prefix}_fetch_failed", category=type(error).__name__
            )
            raise LogUnavailable("History storage unavailable") from error
        finally:
            raise_if_cancelled()


class ValkeyActionRepository(BaseValkeyLogRepository[ActionLog], ActionRepository):
    def __init__(self, redis_url: str):
        super().__init__(
            redis_url, key_prefix="action_log", ttl_seconds=LOG_RETENTION_SECONDS
        )
        self.sequence_key = "action_log_seq"

    async def add_log(self, log: ActionLog) -> None:
        try:
            raise_if_task_stopped()
            # Generate ID
            log_id = await self.redis.incr(self.sequence_key)
            raise_if_task_stopped()
            log.id = log_id

            # Convert to dict for storage
            log_dict = asdict(log)

            await self._add_item(log_dict, log.date.timestamp())
        except LogUnavailable:
            raise
        except Exception as error:
            logger.warning(
                "action_log_add_wrapper_failed", category=type(error).__name__
            )
            raise LogUnavailable("History storage unavailable") from error
        finally:
            raise_if_task_stopped()

    async def get_logs(self, limit: int = 50) -> List[ActionLog]:
        dicts = await self._fetch_items(limit)
        results = []
        try:
            for d in dicts:
                self._restore_date(d)
                results.append(ActionLog(**d))
        except (TypeError, ValueError, KeyError) as error:
            logger.warning("action_log_invalid_record", category=type(error).__name__)
            raise LogUnavailable("History contains invalid records") from error
        return results


class ValkeyEventRepository(BaseValkeyLogRepository[SystemEvent], EventRepository):
    def __init__(self, redis_url: str):
        super().__init__(
            redis_url, key_prefix="system_events", ttl_seconds=LOG_RETENTION_SECONDS
        )

    async def add_event(self, event: SystemEvent) -> None:
        # We don't need to persist 'rendered_html' as it is transient/large
        event_copy = asdict(replace(event, rendered_html=None, message_model=None))

        await self._add_item(event_copy, event.date.timestamp())

    async def get_recent_events(self, limit: int = 10) -> List[SystemEvent]:
        dicts = await self._fetch_items(limit)
        results = []
        try:
            for d in dicts:
                self._restore_date(d)
                if not isinstance(d.get("text"), str):
                    raise ValueError("Invalid event text")
                results.append(SystemEvent(**d))
        except (TypeError, ValueError, KeyError) as error:
            logger.warning(
                "system_events_invalid_record", category=type(error).__name__
            )
            raise LogUnavailable("History contains invalid records") from error
        return results
