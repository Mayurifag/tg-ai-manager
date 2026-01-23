import json
import time
from enum import Enum
from typing import List, Dict

from redis.asyncio import Redis
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class AlertType(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class AlertManager:
    def __init__(self, redis_url: str):
        self.redis = Redis.from_url(redis_url, decode_responses=True)
        self.key = "system:alerts"
        self.ttl = 86400  # 24 hours

    async def add_alert(
        self, type: AlertType, message: str, title: str = "System Alert"
    ):
        alert_id = int(time.time() * 1000)
        payload = {
            "id": alert_id,
            "type": type.value,
            "title": title,
            "message": message,
            "timestamp": time.time(),
        }
        try:
            await self.redis.lpush(self.key, json.dumps(payload))
            await self.redis.ltrim(self.key, 0, 99)  # Keep last 100
            await self.redis.expire(self.key, self.ttl)
        except Exception as e:
            logger.error("add_alert_failed", error=str(e))

    async def get_alerts(self) -> List[Dict]:
        try:
            raw = await self.redis.lrange(self.key, 0, -1)
            return [json.loads(x) for x in raw]
        except Exception:
            return []

    async def clear_alert(self, alert_id: int):
        # O(N) but N is small (max 100)
        alerts = await self.get_alerts()
        new_list = [a for a in alerts if a["id"] != alert_id]

        await self.redis.delete(self.key)
        if new_list:
            # Push back in reverse order to maintain time sort
            for a in reversed(new_list):
                await self.redis.lpush(self.key, json.dumps(a))
