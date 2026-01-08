from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    VALKEY_URL: str = "redis://valkey:6379/0"
    DB_PATH: str = "data.db"

def get_settings():
    return Settings()  # type: ignore
