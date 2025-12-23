import sqlite3
import asyncio
from typing import Callable, TypeVar, Any

T = TypeVar("T")

class BaseSqliteRepository:
    """
    Base repository handling SQLite connection creation and 
    async execution offloading.
    """
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        """Create a new synchronous connection to the database."""
        return sqlite3.connect(self.db_path)

    async def _execute(self, func: Callable[[], T]) -> T:
        """Run a synchronous database operation in a separate thread."""
        return await asyncio.to_thread(func)