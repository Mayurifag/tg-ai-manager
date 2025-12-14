from src.config import get_settings
from src.adapters.telegram import TelethonAdapter
from src.services import ChatService

_service_instance = None

def get_chat_service() -> ChatService:
    global _service_instance
    if _service_instance is None:
        settings = get_settings()
        adapter = TelethonAdapter(
            settings.TG_SESSION_NAME,
            settings.TG_API_ID,
            settings.TG_API_HASH
        )
        _service_instance = ChatService(adapter)
    return _service_instance