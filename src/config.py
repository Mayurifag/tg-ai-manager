from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    VALKEY_URL: str = "redis://valkey:6379/0"
    DB_PATH: str = "data.db"

    TG_API_ID: int
    TG_API_HASH: str

    WRITE_QUEUE_DELAY: float = 0.5

    # When set, sync rules from production export URL on startup
    RULES_SYNC_URL: Optional[str] = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore
