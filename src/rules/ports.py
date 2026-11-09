from abc import ABC, abstractmethod
from typing import List, Optional

from src.rules.models import Rule, RuleType


class InheritedRuleError(ValueError):
    """A topic cannot be disabled while its parent rule remains enabled."""


class InvalidRuleConfiguration(ValueError):
    """Stored rule configuration cannot be interpreted safely."""

    def __init__(self, rule_id: Optional[int] = None):
        super().__init__("Stored rule configuration is invalid")
        self.rule_id = rule_id


class RuleRepository(ABC):
    @abstractmethod
    async def set_scope(
        self, rule: Rule, enabled: bool, *, update_config: bool = False
    ) -> Optional[Rule]:
        """Atomically change one scope and return its effective rule."""
        pass

    @abstractmethod
    async def set_forum_autoread(
        self, user_id: int, chat_id: int, enabled: bool
    ) -> None:
        """Atomically enable the parent or disable every topic in the forum."""
        pass

    @abstractmethod
    async def get_by_chat_and_topic(
        self, chat_id: int, topic_id: Optional[int] = None
    ) -> List[Rule]:
        pass

    @abstractmethod
    async def get_all_for_chat(self, chat_id: int) -> List[Rule]:
        pass

    @abstractmethod
    async def get_all(self, *, rule_type: Optional[RuleType] = None) -> List[Rule]:
        """Retrieve rules, optionally limited to one type."""
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

    @abstractmethod
    async def delete_all(self) -> None:
        """Delete all rules from the database."""
        pass
