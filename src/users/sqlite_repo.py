import json
from typing import Any, Optional

from src.config import get_settings
from src.infrastructure.db import BaseSqliteRepository
from src.infrastructure.security import CryptoManager
from src.rules.models import Rule
from src.users.models import User
from src.users.ports import UserRepository


class SqliteUserRepository(BaseSqliteRepository, UserRepository):
    _UPDATE_FIELDS = frozenset(
        {
            "username",
            "session_string",
            "is_premium",
            "autoread_service_messages",
            "autoread_polls",
            "autoread_self",
            "autoread_bots",
            "autoread_regex",
            "debug_mode",
            "ai_provider",
            "ai_model",
            "ai_api_key",
            "ai_prompt",
        }
    )
    _SECRET_FIELDS = frozenset({"session_string", "ai_api_key"})

    def __init__(self, db_path: str = "data.db"):
        super().__init__(db_path)
        self.crypto = CryptoManager()
        self.settings = get_settings()

    async def get_user(self, user_id: int = 1) -> Optional[User]:
        def _fetch():
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    SELECT id, username, session_string,
                           autoread_service_messages, autoread_polls, autoread_self,
                           autoread_bots, autoread_regex, is_premium, debug_mode,
                           ai_provider, ai_model, ai_api_key, ai_prompt
                    FROM users WHERE id = ?
                """,
                    (user_id,),
                )
                row = cursor.fetchone()

                if row:
                    return User(
                        id=row["id"],
                        api_id=self.settings.TG_API_ID,
                        api_hash=self.settings.TG_API_HASH,
                        username=row["username"],
                        session_string=self.crypto.decrypt(row["session_string"]),
                        autoread_service_messages=bool(
                            row["autoread_service_messages"]
                        ),
                        autoread_polls=bool(row["autoread_polls"]),
                        autoread_self=bool(row["autoread_self"]),
                        autoread_bots=row["autoread_bots"] or "",
                        autoread_regex=row["autoread_regex"] or "",
                        is_premium=bool(row["is_premium"]),
                        debug_mode=bool(row["debug_mode"]),
                        ai_provider=row["ai_provider"],
                        ai_model=row["ai_model"],
                        ai_api_key=self.crypto.decrypt(row["ai_api_key"]),
                        ai_prompt=row["ai_prompt"],
                    )
                return None

        return await self._execute(_fetch)

    async def save_user(self, user: User) -> None:
        enc_session = self.crypto.encrypt(user.session_string)

        def _save():
            with self._connect() as conn:
                cursor = conn.execute("SELECT 1 FROM users WHERE id = ?", (user.id,))
                exists = cursor.fetchone()

                if exists:
                    conn.execute(
                        """
                        UPDATE users
                        SET username = ?, session_string = ?,
                            autoread_service_messages = ?, autoread_polls = ?, autoread_self = ?,
                            autoread_bots = ?, autoread_regex = ?, is_premium = ?, debug_mode = ?,
                            ai_provider = ?, ai_model = ?, ai_api_key = ?, ai_prompt = ?
                        WHERE id = ?
                        """,
                        (
                            user.username,
                            enc_session,
                            int(user.autoread_service_messages),
                            int(user.autoread_polls),
                            int(user.autoread_self),
                            user.autoread_bots,
                            user.autoread_regex,
                            int(user.is_premium),
                            int(user.debug_mode),
                            user.ai_provider,
                            user.ai_model,
                            self.crypto.encrypt(user.ai_api_key),
                            user.ai_prompt,
                            user.id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO users (
                            id, username, session_string,
                            autoread_service_messages, autoread_polls, autoread_self,
                            autoread_bots, autoread_regex, is_premium, debug_mode,
                            ai_provider, ai_model, ai_api_key, ai_prompt
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            user.id,
                            user.username,
                            enc_session,
                            int(user.autoread_service_messages),
                            int(user.autoread_polls),
                            int(user.autoread_self),
                            user.autoread_bots,
                            user.autoread_regex,
                            int(user.is_premium),
                            int(user.debug_mode),
                            user.ai_provider,
                            user.ai_model,
                            self.crypto.encrypt(user.ai_api_key),
                            user.ai_prompt,
                        ),
                    )

        await self._execute(_save)

    async def update_fields(
        self, user_id: int, fields: dict[str, Any], *, expected_session: Optional[str]
    ) -> bool:
        if not fields or not fields.keys() <= self._UPDATE_FIELDS:
            raise ValueError("Invalid user fields")
        values = {
            name: self.crypto.encrypt(value) if name in self._SECRET_FIELDS else value
            for name, value in fields.items()
        }

        def _update() -> bool:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT session_string FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                if (
                    not row
                    or self.crypto.decrypt(row["session_string"]) != expected_session
                ):
                    return False
                assignments = ", ".join(f"{field} = ?" for field in values)
                conn.execute(
                    f"UPDATE users SET {assignments} WHERE id = ?",
                    (*values.values(), user_id),
                )
                return True

        return await self._execute(_update)

    async def replace_rules_and_settings(
        self,
        user_id: int,
        rules: list[Rule],
        fields: dict[str, Any],
        *,
        expected_session: Optional[str],
    ) -> bool:
        allowed = self._UPDATE_FIELDS - self._SECRET_FIELDS - {"username", "is_premium"}
        if not fields.keys() <= allowed or any(
            rule.user_id != user_id for rule in rules
        ):
            raise ValueError("Invalid import fields")

        def _replace() -> bool:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT session_string FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                if (
                    not row
                    or self.crypto.decrypt(row["session_string"]) != expected_session
                ):
                    return False
                conn.execute("DELETE FROM rules WHERE user_id = ?", (user_id,))
                for rule in rules:
                    conn.execute(
                        "INSERT INTO rules (user_id, rule_type, chat_id, topic_id, config, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            user_id,
                            rule.rule_type.value,
                            rule.chat_id,
                            rule.topic_id,
                            json.dumps(rule.config),
                            rule.created_at.isoformat(),
                            rule.updated_at.isoformat(),
                        ),
                    )
                if fields:
                    assignments = ", ".join(f"{field} = ?" for field in fields)
                    conn.execute(
                        f"UPDATE users SET {assignments} WHERE id = ?",
                        (*fields.values(), user_id),
                    )
                return True

        return await self._execute(_replace)

    async def delete_user(self, user_id: int) -> None:
        def _delete():
            with self._connect() as conn:
                conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

        await self._execute(_delete)
