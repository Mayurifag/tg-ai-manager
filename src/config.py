from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    VALKEY_URL: str = "redis://valkey:6379/0"
    DB_PATH: str = "data.db"

    TG_API_ID: int
    TG_API_HASH: str


def get_settings():
    return Settings()  # type: ignore
