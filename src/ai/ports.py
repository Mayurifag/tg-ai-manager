from abc import ABC, abstractmethod


class AIClassifier(ABC):
    @abstractmethod
    async def classify_is_ad(self, text: str) -> bool:
        """Return True if the given text is an advertisement, False otherwise."""
        ...
