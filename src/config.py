from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    TG_API_ID: int
    TG_API_HASH: str
    TG_SESSION_NAME: str = "manager_session"

    class Config:
        env_file = ".env"

def get_settings():
    return Settings() # type: ignore
