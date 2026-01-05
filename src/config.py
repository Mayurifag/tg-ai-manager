from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    TG_API_ID: int
    TG_API_HASH: str
    TG_SESSION_NAME: str = "manager_session"

    # Valkey / Redis Configuration
    VALKEY_URL: str = "redis://127.0.0.1:6379/0"

    class Config:
        env_file = ".env"

def get_settings():
    return Settings() # type: ignore
