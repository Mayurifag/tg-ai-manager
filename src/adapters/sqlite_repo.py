import sqlite3
from datetime import datetime
from typing import List
from src.domain.ports import ActionRepository
from src.domain.models import ActionLog
from src.infrastructure.db import BaseSqliteRepository

class SqliteActionRepository(BaseSqliteRepository, ActionRepository):
    def __init__(self, db_path: str = "actions.db"):
        super().__init__(db_path)
        self._init_db()

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS action_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    chat_name TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    date TEXT NOT NULL,
                    link TEXT
                )
            """)
            conn.commit()

    async def add_log(self, log: ActionLog) -> None:
        def _insert():
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO action_logs (action, chat_id, chat_name, reason, date, link) VALUES (?, ?, ?, ?, ?, ?)",
                    (log.action, log.chat_id, log.chat_name, log.reason, log.date.isoformat(), log.link)
                )
                conn.commit()

        await self._execute(_insert)

    async def get_logs(self, limit: int = 50) -> List[ActionLog]:
        def _fetch():
            results = []
            with self._connect() as conn:
                cursor = conn.execute(
                    "SELECT id, action, chat_id, chat_name, reason, date, link FROM action_logs ORDER BY date DESC LIMIT ?",
                    (limit,)
                )
                for row in cursor:
                    results.append(ActionLog(
                        id=row[0],
                        action=row[1],
                        chat_id=row[2],
                        chat_name=row[3],
                        reason=row[4],
                        date=datetime.fromisoformat(row[5]),
                        link=row[6] if len(row) > 6 else None
                    ))
            return results

        return await self._execute(_fetch)
