from abc import ABC, abstractmethod
from src.settings.models import GlobalSettings


class SettingsRepository(ABC):
    @abstractmethod
    async def get_settings(self) -> GlobalSettings:
        pass

    @abstractmethod
    async def save_settings(self, settings: GlobalSettings) -> None:
        pass
