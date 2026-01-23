from src.infrastructure.db import BaseSqliteRepository
from src.settings.models import GlobalSettings
from src.settings.ports import SettingsRepository


class SqliteSettingsRepository(BaseSqliteRepository, SettingsRepository):
    def __init__(self, db_path: str = "data.db"):
        super().__init__(db_path)
        # _init_db is handled by Alembic mostly now, but we ensure the row exists
        self._ensure_row()

    def _ensure_row(self):
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO global_settings (id) VALUES (1)")
            conn.commit()

    async def get_settings(self) -> GlobalSettings:
        def _fetch():
            with self._connect() as conn:
                try:
                    cursor = conn.execute("""
                        SELECT autoread_service_messages, autoread_polls,
                               autoread_bots, autoread_regex, autoread_self,
                               ai_enabled, ai_provider, ai_model, ai_api_key, ai_base_url,
                               skip_ads_enabled
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
                            autoread_self=bool(row[4]),
                            ai_enabled=bool(row[5]),
                            ai_provider=row[6] or "gemini",
                            ai_model=row[7] or "gemini-pro",
                            ai_api_key=row[8],
                            ai_base_url=row[9],
                            skip_ads_enabled=bool(row[10]),
                        )
                except Exception:
                    pass
                return GlobalSettings()

        return await self._execute(_fetch)

    async def save_settings(self, settings: GlobalSettings) -> None:
        def _save():
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE global_settings
                    SET autoread_service_messages = ?,
                        autoread_polls = ?,
                        autoread_bots = ?,
                        autoread_regex = ?,
                        autoread_self = ?,
                        ai_enabled = ?,
                        ai_provider = ?,
                        ai_model = ?,
                        ai_api_key = ?,
                        ai_base_url = ?,
                        skip_ads_enabled = ?
                    WHERE id = 1
                """,
                    (
                        int(settings.autoread_service_messages),
                        int(settings.autoread_polls),
                        settings.autoread_bots,
                        settings.autoread_regex,
                        int(settings.autoread_self),
                        int(settings.ai_enabled),
                        settings.ai_provider,
                        settings.ai_model,
                        settings.ai_api_key,
                        settings.ai_base_url,
                        int(settings.skip_ads_enabled),
                    ),
                )
                conn.commit()

        await self._execute(_save)
