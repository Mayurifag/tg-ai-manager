from abc import ABC, abstractmethod
from typing import List, Optional
from src.rules.models import Rule


class RuleRepository(ABC):
    @abstractmethod
    async def get_by_chat_and_topic(
        self, chat_id: int, topic_id: Optional[int] = None
    ) -> List[Rule]:
        pass

    @abstractmethod
    async def get_all_for_chat(self, chat_id: int) -> List[Rule]:
        pass

    @abstractmethod
    async def add(self, rule: Rule) -> int:
        pass

    @abstractmethod
    async def update(self, rule: Rule) -> None:
        pass

    @abstractmethod
    async def delete(self, rule_id: int) -> None:
        pass
