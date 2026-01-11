import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.infrastructure.db import BaseSqliteRepository
from src.rules.models import Rule, RuleType
from src.rules.ports import RuleRepository


class SqliteRuleRepository(BaseSqliteRepository, RuleRepository):
    def __init__(self, db_path: str = "data.db"):
        super().__init__(db_path)

    def _parse_config(self, config_str: Optional[str]) -> Dict[str, Any]:
        if not config_str:
            return {}
        try:
            return json.loads(config_str)
        except json.JSONDecodeError:
            return {}

    async def get_by_chat_and_topic(
        self, chat_id: int, topic_id: Optional[int] = None
    ) -> List[Rule]:
        def _fetch():
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    SELECT id, user_id, rule_type, chat_id, topic_id, config, created_at, updated_at
                    FROM rules
                    WHERE chat_id = ? AND (topic_id = ? OR topic_id IS NULL)
                    ORDER BY topic_id DESC NULLS LAST
                """,
                    (chat_id, topic_id),
                )
                results = []
                for row in cursor:
                    rule = Rule(
                        id=row[0],
                        user_id=row[1],
                        rule_type=RuleType(row[2]),
                        chat_id=row[3],
                        topic_id=row[4],
                        config=self._parse_config(row[5]),
                        created_at=datetime.fromisoformat(row[6]),
                        updated_at=datetime.fromisoformat(row[7]),
                    )
                    results.append(rule)
                return results

        return await self._execute(_fetch)

    async def get_all_for_chat(self, chat_id: int) -> List[Rule]:
        def _fetch():
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    SELECT id, user_id, rule_type, chat_id, topic_id, config, created_at, updated_at
                    FROM rules WHERE chat_id = ?
                """,
                    (chat_id,),
                )
                results = []
                for row in cursor:
                    rule = Rule(
                        id=row[0],
                        user_id=row[1],
                        rule_type=RuleType(row[2]),
                        chat_id=row[3],
                        topic_id=row[4],
                        config=self._parse_config(row[5]),
                        created_at=datetime.fromisoformat(row[6]),
                        updated_at=datetime.fromisoformat(row[7]),
                    )
                    results.append(rule)
                return results

        return await self._execute(_fetch)

    async def get_all(self) -> List[Rule]:
        def _fetch():
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    SELECT id, user_id, rule_type, chat_id, topic_id, config, created_at, updated_at
                    FROM rules
                    ORDER BY chat_id ASC, topic_id ASC NULLS FIRST
                """
                )
                results = []
                for row in cursor:
                    rule = Rule(
                        id=row[0],
                        user_id=row[1],
                        rule_type=RuleType(row[2]),
                        chat_id=row[3],
                        topic_id=row[4],
                        config=self._parse_config(row[5]),
                        created_at=datetime.fromisoformat(row[6]),
                        updated_at=datetime.fromisoformat(row[7]),
                    )
                    results.append(rule)
                return results

        return await self._execute(_fetch)

    async def add(self, rule: Rule) -> int:
        def _insert():
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO rules (user_id, rule_type, chat_id, topic_id, config, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        rule.user_id,
                        rule.rule_type.value,
                        rule.chat_id,
                        rule.topic_id,
                        json.dumps(rule.config),
                        rule.created_at.isoformat(),
                        rule.updated_at.isoformat(),
                    ),
                )
                conn.commit()
                if cursor.lastrowid is None:
                    raise ValueError("Database insert failed: no ID returned")
                return cursor.lastrowid

        return await self._execute(_insert)

    async def update(self, rule: Rule) -> None:
        def _update():
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE rules SET updated_at = ?, config = ?
                    WHERE id = ?
                """,
                    (datetime.now().isoformat(), json.dumps(rule.config), rule.id),
                )
                conn.commit()

        await self._execute(_update)

    async def delete(self, rule_id: int) -> None:
        def _delete():
            with self._connect() as conn:
                conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
                conn.commit()

        await self._execute(_delete)
