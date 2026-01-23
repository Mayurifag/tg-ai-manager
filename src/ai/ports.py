from abc import ABC, abstractmethod
from typing import List


class AIProvider(ABC):
    @abstractmethod
    async def complete(self, prompt: str, system: str = None) -> str:
        """Generates a text completion."""
        pass

    @abstractmethod
    async def classify(self, text: str, categories: List[str]) -> str:
        """
        Classifies text into one of the provided categories.
        Returns the category string that matches best.
        """
        pass
