from typing import Optional

from src.config import get_settings
from src.infrastructure.db import BaseSqliteRepository
from src.infrastructure.security import CryptoManager
from src.users.models import User
from src.users.ports import UserRepository


class SqliteUserRepository(BaseSqliteRepository, UserRepository):
    def __init__(self, db_path: str = "data.db"):
        super().__init__(db_path)
        self.crypto = CryptoManager()
        self.settings = get_settings()

    async def get_user(self, user_id: int = 1) -> Optional[User]:
        def _fetch():
            with self._connect() as conn:
                try:
                    cursor = conn.execute(
                        """
                        SELECT id, username, session_string,
                               autoread_service_messages, autoread_polls, autoread_self,
                               autoread_bots, autoread_regex, is_premium, debug_mode
                        FROM users WHERE id = ?
                    """,
                        (user_id,),
                    )
                    row = cursor.fetchone()

                    if row:
                        return User(
                            id=row[0],
                            api_id=self.settings.TG_API_ID,
                            api_hash=self.settings.TG_API_HASH,
                            username=row[1],
                            session_string=self.crypto.decrypt(row[2]),
                            autoread_service_messages=bool(row[3]),
                            autoread_polls=bool(row[4]),
                            autoread_self=bool(row[5]),
                            autoread_bots=row[6] or "",
                            autoread_regex=row[7] or "",
                            is_premium=bool(row[8]),
                            debug_mode=bool(row[9]),
                        )
                except Exception:
                    pass
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
                            autoread_bots = ?, autoread_regex = ?, is_premium = ?, debug_mode = ?
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
                            user.id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO users (
                            id, username, session_string,
                            autoread_service_messages, autoread_polls, autoread_self,
                            autoread_bots, autoread_regex, is_premium, debug_mode
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        ),
                    )
                conn.commit()

        await self._execute(_save)

    async def delete_user(self, user_id: int) -> None:
        def _delete():
            with self._connect() as conn:
                conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
                conn.commit()

        await self._execute(_delete)
