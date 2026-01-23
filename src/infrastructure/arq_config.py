from arq.connections import RedisSettings
from src.config import get_settings


def get_redis_settings() -> RedisSettings:
    settings = get_settings()
    # Arq natively parses redis:// strings via from_dsn
    return RedisSettings.from_dsn(settings.VALKEY_URL)
