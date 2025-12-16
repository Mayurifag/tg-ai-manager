from src.config import get_settings
from src.adapters.telegram import TelethonAdapter
from src.adapters.sqlite_repo import SqliteActionRepository
from src.application.interactors import ChatInteractor

_interactor_instance = None

def get_chat_interactor() -> ChatInteractor:
    global _interactor_instance
    if _interactor_instance is None:
        settings = get_settings()

        # Telegram Adapter
        tg_adapter = TelethonAdapter(
            settings.TG_SESSION_NAME,
            settings.TG_API_ID,
            settings.TG_API_HASH
        )

        # SQLite Action Repository
        action_repo = SqliteActionRepository("actions.db")

        _interactor_instance = ChatInteractor(tg_adapter, action_repo)

    return _interactor_instance
