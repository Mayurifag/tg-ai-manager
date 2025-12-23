from src.infrastructure.db import BaseSqliteRepository
from src.settings.models import GlobalSettings
from src.settings.ports import SettingsRepository

class SqliteSettingsRepository(BaseSqliteRepository, SettingsRepository):
    def __init__(self, db_path: str = "settings.db"):
        super().__init__(db_path)
        self._init_db()

    def _init_db(self):
        with self._connect() as conn:
            # We removed autoread_only_new.
            # SQLite doesn't support DROP COLUMN easily, so for a new setup this is fine.
            # For existing setup, the extra column will just be ignored by the code below.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS global_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    autoread_service_messages INTEGER DEFAULT 0,
                    autoread_polls INTEGER DEFAULT 0,
                    autoread_bots TEXT DEFAULT '@lolsBotCatcherBot',
                    autoread_regex TEXT DEFAULT '',
                    autoread_self INTEGER DEFAULT 0
                )
            """)
            # Ensure default row exists
            conn.execute("""
                INSERT OR IGNORE INTO global_settings (id) VALUES (1)
            """)
            conn.commit()

    async def get_settings(self) -> GlobalSettings:
        def _fetch():
            with self._connect() as conn:
                # We simply don't select autoread_only_new even if it exists in old DBs
                try:
                    cursor = conn.execute("""
                        SELECT autoread_service_messages, autoread_polls,
                               autoread_bots, autoread_regex, autoread_self
                        FROM global_settings WHERE id = 1
                    """)
                    row = cursor.fetchone()

                    if row:
                        return GlobalSettings(
                            id=1,
                            autoread_service_messages=bool(row[0]),
                            autoread_polls=bool(row[1]),
                            autoread_bots=row[2] or "",
                            autoread_regex=row[3] or "",
                            autoread_self=bool(row[4])
                        )
                except Exception:
                    # Fallback if table structure is very different (shouldn't happen with IF EXISTS)
                    pass
                return GlobalSettings()
        return await self._execute(_fetch)

    async def save_settings(self, settings: GlobalSettings) -> None:
        def _save():
            with self._connect() as conn:
                # Handle potential missing column in older DBs by just trying the update
                # In a real migration scenario we'd alter table, but here we just update what matches
                conn.execute("""
                    UPDATE global_settings
                    SET autoread_service_messages = ?,
                        autoread_polls = ?,
                        autoread_bots = ?,
                        autoread_regex = ?,
                        autoread_self = ?
                    WHERE id = 1
                """, (
                    int(settings.autoread_service_messages),
                    int(settings.autoread_polls),
                    settings.autoread_bots,
                    settings.autoread_regex,
                    int(settings.autoread_self)
                ))
                conn.commit()
        await self._execute(_save)
