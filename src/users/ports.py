from abc import ABC, abstractmethod
from typing import Optional
from typing import Any
from src.rules.models import Rule
from src.users.models import User


class UserRepository(ABC):
    @abstractmethod
    async def get_user(self, user_id: int = 1) -> Optional[User]:
        pass

    @abstractmethod
    async def create_user(self, user: User) -> None:
        """Create an account without overwriting an existing one."""
        pass

    @abstractmethod
    async def update_fields(
        self, user_id: int, fields: dict[str, Any], *, expected_session: Optional[str]
    ) -> bool:
        """Update owned fields only if this is still the observed account session."""
        pass

    @abstractmethod
    async def replace_rules_and_settings(
        self,
        user_id: int,
        rules: list[Rule],
        fields: dict[str, Any],
        *,
        expected_session: Optional[str],
    ) -> bool:
        """Replace one account's rules and settings in the same transaction."""
        pass

    @abstractmethod
    async def delete_user(self, user_id: int) -> None:
        """Deletes the user and cascades to related data."""
        pass
