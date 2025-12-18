import sqlite3
import asyncio
from typing import List, Optional
from datetime import datetime
from src.rules.models import Rule, RuleType, AutoReadRule
from src.rules.ports import RuleRepository

class SqliteRuleRepository(RuleRepository):
    def __init__(self, db_path: str = "rules.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_type TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    topic_id INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_topic ON rules(chat_id, topic_id)")
            conn.commit()

    async def get_by_chat_and_topic(self, chat_id: int, topic_id: Optional[int] = None) -> List[Rule]:
        def _fetch():
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    SELECT id, rule_type, chat_id, topic_id, enabled, created_at, updated_at
                    FROM rules
                    WHERE chat_id = ? AND (topic_id = ? OR topic_id IS NULL)
                    ORDER BY topic_id DESC NULLS LAST
                """, (chat_id, topic_id))
                results = []
                for row in cursor:
                    rule = Rule(
                        id=row[0],
                        rule_type=RuleType(row[1]),
                        chat_id=row[2],
                        topic_id=row[3],
                        enabled=bool(row[4]),
                        created_at=datetime.fromisoformat(row[5]),
                        updated_at=datetime.fromisoformat(row[6])
                    )
                    results.append(rule)
                return results
        return await asyncio.to_thread(_fetch)

    async def get_all_for_chat(self, chat_id: int) -> List[Rule]:
        def _fetch():
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    SELECT id, rule_type, chat_id, topic_id, enabled, created_at, updated_at
                    FROM rules WHERE chat_id = ?
                """, (chat_id,))
                results = []
                for row in cursor:
                    rule = Rule(
                        id=row[0],
                        rule_type=RuleType(row[1]),
                        chat_id=row[2],
                        topic_id=row[3],
                        enabled=bool(row[4]),
                        created_at=datetime.fromisoformat(row[5]),
                        updated_at=datetime.fromisoformat(row[6])
                    )
                    results.append(rule)
                return results
        return await asyncio.to_thread(_fetch)

    async def add(self, rule: Rule) -> int:
        def _insert():
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    INSERT INTO rules (rule_type, chat_id, topic_id, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    rule.rule_type.value,
                    rule.chat_id,
                    rule.topic_id,
                    int(rule.enabled),
                    rule.created_at.isoformat(),
                    rule.updated_at.isoformat()
                ))
                conn.commit()
                return cursor.lastrowid
        return await asyncio.to_thread(_insert)

    async def update(self, rule: Rule) -> None:
        def _update():
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    UPDATE rules SET enabled = ?, updated_at = ?
                    WHERE id = ?
                """, (int(rule.enabled), datetime.now().isoformat(), rule.id))
                conn.commit()
        await asyncio.to_thread(_update)

    async def delete(self, rule_id: int) -> None:
        def _delete():
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
                conn.commit()
        await asyncio.to_thread(_delete)
