from abc import ABC, abstractmethod
from typing import Optional
from src.users.models import User


class UserRepository(ABC):
    @abstractmethod
    async def get_user(self, user_id: int = 1) -> Optional[User]:
        pass

    @abstractmethod
    async def save_user(self, user: User) -> None:
        """Creates or Updates the user."""
        pass

    @abstractmethod
    async def delete_user(self, user_id: int) -> None:
        """Deletes the user and cascades to related data."""
        pass
