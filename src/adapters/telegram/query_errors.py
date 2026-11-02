from telethon.errors import FloodWaitError

from src.domain.errors import TelegramUnavailable


def unavailable(error: Exception) -> TelegramUnavailable:
    if isinstance(error, FloodWaitError):
        return TelegramUnavailable(retry_after=max(1, error.seconds))
    return TelegramUnavailable()
