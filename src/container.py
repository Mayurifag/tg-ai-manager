from src.config import get_settings
from src.adapters.telegram import TelethonAdapter
from src.adapters.sqlite_repo import SqliteActionRepository
from src.rules.sqlite_repo import SqliteRuleRepository
from src.settings.sqlite_repo import SqliteSettingsRepository
from src.application.interactors import ChatInteractor
from src.rules.service import RuleService

# Singleton instances
_tg_adapter: TelethonAdapter | None = None
_action_repo: SqliteActionRepository | None = None
_settings_repo: SqliteSettingsRepository | None = None
_interactor: ChatInteractor | None = None
_rule_service: RuleService | None = None

def _get_tg_adapter() -> TelethonAdapter:
    global _tg_adapter
    if _tg_adapter is None:
        settings = get_settings()
        _tg_adapter = TelethonAdapter(
            settings.TG_SESSION_NAME,
            settings.TG_API_ID,
            settings.TG_API_HASH
        )
    return _tg_adapter

def _get_action_repo() -> SqliteActionRepository:
    global _action_repo
    if _action_repo is None:
        _action_repo = SqliteActionRepository("actions.db")
    return _action_repo

def _get_settings_repo() -> SqliteSettingsRepository:
    global _settings_repo
    if _settings_repo is None:
        _settings_repo = SqliteSettingsRepository("settings.db")
    return _settings_repo

def get_chat_interactor() -> ChatInteractor:
    global _interactor
    if _interactor is None:
        _interactor = ChatInteractor(_get_tg_adapter(), _get_action_repo())
    return _interactor

def get_rule_service() -> RuleService:
    global _rule_service
    if _rule_service is None:
        rule_repo = SqliteRuleRepository("rules.db")
        _rule_service = RuleService(
            rule_repo,
            _get_action_repo(),
            _get_tg_adapter(),
            _get_settings_repo()
        )
    return _rule_service
