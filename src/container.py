from src.config import get_settings
from src.adapters.telegram import TelethonAdapter
from src.application.interactors import ChatInteractor

_interactor_instance = None

def get_chat_interactor() -> ChatInteractor:
    global _interactor_instance
    if _interactor_instance is None:
        settings = get_settings()
        adapter = TelethonAdapter(
            settings.TG_SESSION_NAME,
            settings.TG_API_ID,
            settings.TG_API_HASH
        )
        _interactor_instance = ChatInteractor(adapter)
    return _interactor_instance
