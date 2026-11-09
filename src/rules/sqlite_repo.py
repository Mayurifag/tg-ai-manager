import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.infrastructure.db import BaseSqliteRepository
from src.infrastructure.json_data import load_json
from src.requests import BadRequest
from src.rules.models import Rule, RuleType
from src.rules.ports import InheritedRuleError, InvalidRuleConfiguration, RuleRepository
from src.rules.validation import stored_autoreact_options


class SqliteRuleRepository(BaseSqliteRepository, RuleRepository):
    def __init__(self, db_path: str = "data.db"):
        super().__init__(db_path)

    def _parse_config(
        self, config_str: Optional[str], rule_id: Optional[int] = None
    ) -> Dict[str, Any]:
        if config_str is None or config_str == "":
            return {}
        try:
            config = load_json(config_str)
        except (ValueError, TypeError) as error:
            raise InvalidRuleConfiguration(rule_id) from error
        if not isinstance(config, dict):
            raise InvalidRuleConfiguration(rule_id)
        return config

    def _row_to_rule(self, row) -> Rule:
        rule = Rule(
            id=row["id"],
            user_id=row["user_id"],
            rule_type=RuleType(row["rule_type"]),
            chat_id=row["chat_id"],
            topic_id=row["topic_id"],
            config=self._parse_config(row["config"], row["id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
        if rule.rule_type == RuleType.AUTOREACT:
            try:
                stored_autoreact_options(rule)
            except BadRequest as error:
                raise InvalidRuleConfiguration(rule.id) from error
        return rule

    async def set_scope(
        self, rule: Rule, enabled: bool, *, update_config: bool = False
    ) -> Optional[Rule]:
        def _set():
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                scope = (
                    rule.user_id,
                    rule.rule_type.value,
                    rule.chat_id,
                    rule.topic_id,
                )
                if enabled:
                    now = datetime.now().isoformat()
                    conn.execute(
                        "INSERT INTO rules (user_id, rule_type, chat_id, topic_id, config, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                        (*scope, json.dumps(rule.config), now, now),
                    )
                    if update_config:
                        conn.execute(
                            "UPDATE rules SET config = ?, updated_at = ? "
                            "WHERE user_id = ? AND rule_type = ? AND chat_id = ? AND topic_id IS ?",
                            (json.dumps(rule.config), now, *scope),
                        )
                else:
                    if (
                        rule.topic_id is not None
                        and conn.execute(
                            "SELECT 1 FROM rules WHERE user_id = ? AND rule_type = ? "
                            "AND chat_id = ? AND topic_id IS NULL",
                            scope[:3],
                        ).fetchone()
                    ):
                        raise InheritedRuleError(
                            "This topic inherits an enabled forum rule; disable the forum rule first."
                        )
                    conn.execute(
                        "DELETE FROM rules WHERE user_id = ? AND rule_type = ? AND chat_id = ? AND topic_id IS ?",
                        scope,
                    )
                row = conn.execute(
                    "SELECT * FROM rules WHERE user_id = ? AND rule_type = ? AND chat_id = ? "
                    "AND (topic_id IS ? OR topic_id IS NULL) ORDER BY topic_id DESC NULLS LAST LIMIT 1",
                    scope,
                ).fetchone()
                return self._row_to_rule(row) if row else None

        return await self._execute(_set)

    async def set_forum_autoread(
        self, user_id: int, chat_id: int, enabled: bool
    ) -> None:
        def _set():
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if enabled:
                    now = datetime.now().isoformat()
                    conn.execute(
                        "INSERT INTO rules (user_id, rule_type, chat_id, topic_id, config, created_at, updated_at) "
                        "VALUES (?, ?, ?, NULL, ?, ?, ?) ON CONFLICT DO NOTHING",
                        (user_id, RuleType.AUTOREAD.value, chat_id, "{}", now, now),
                    )
                else:
                    conn.execute(
                        "DELETE FROM rules WHERE user_id = ? AND chat_id = ? AND rule_type = ?",
                        (user_id, chat_id, RuleType.AUTOREAD.value),
                    )

        await self._execute(_set)

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
                return [self._row_to_rule(row) for row in cursor]

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
                return [self._row_to_rule(row) for row in cursor]

        return await self._execute(_fetch)

    async def get_all(self, *, rule_type: Optional[RuleType] = None) -> List[Rule]:
        def _fetch():
            with self._connect() as conn:
                kind = rule_type.value if rule_type is not None else None
                cursor = conn.execute(
                    """
                    SELECT id, user_id, rule_type, chat_id, topic_id, config, created_at, updated_at
                    FROM rules
                    WHERE (? IS NULL OR rule_type = ?)
                    ORDER BY chat_id ASC, topic_id ASC NULLS FIRST
                """,
                    (kind, kind),
                )
                return [self._row_to_rule(row) for row in cursor]

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

        await self._execute(_update)

    async def delete(self, rule_id: int) -> None:
        def _delete():
            with self._connect() as conn:
                conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))

        await self._execute(_delete)

    async def delete_all(self) -> None:
        def _delete_all():
            with self._connect() as conn:
                conn.execute("DELETE FROM rules")

        await self._execute(_delete_all)
