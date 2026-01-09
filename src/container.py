import sqlite3
from src.config import get_settings
from src.adapters.telegram import TelethonAdapter
from src.adapters.valkey_repo import ValkeyActionRepository, ValkeyEventRepository
from src.rules.sqlite_repo import SqliteRuleRepository
from src.users.sqlite_repo import SqliteUserRepository
from src.application.interactors import ChatInteractor
from src.rules.service import RuleService
from src.infrastructure.security import CryptoManager

_tg_adapter: TelethonAdapter | None = None
_action_repo: ValkeyActionRepository | None = None
_event_repo: ValkeyEventRepository | None = None
_user_repo: SqliteUserRepository | None = None
_interactor: ChatInteractor | None = None
_rule_service: RuleService | None = None


def _get_tg_adapter() -> TelethonAdapter:
    global _tg_adapter
    if _tg_adapter is not None:
        return _tg_adapter

    settings = get_settings()
    crypto = CryptoManager()
    session_string = None

    try:
        with sqlite3.connect(settings.DB_PATH) as conn:
            cur = conn.execute("SELECT session_string FROM users WHERE id = 1")
            row = cur.fetchone()
            if row and row[0]:
                session_string = crypto.decrypt(row[0])
    except Exception as e:
        print(f"Error fetching session for adapter: {e}")

    _tg_adapter = TelethonAdapter(
        session_string=session_string,
        api_id=settings.TG_API_ID,
        api_hash=settings.TG_API_HASH,
    )
    return _tg_adapter


def reload_tg_adapter(
    api_id: int = None, api_hash: str = None, session_string: str = None
):
    global _tg_adapter
    if _tg_adapter:
        pass

    settings = get_settings()

    _tg_adapter = TelethonAdapter(
        session_string=session_string,
        api_id=settings.TG_API_ID,
        api_hash=settings.TG_API_HASH,
    )

    global _interactor
    if _interactor:
        _interactor.repository = _tg_adapter

    global _rule_service
    if _rule_service:
        _rule_service.chat_repo = _tg_adapter


def get_action_repo() -> ValkeyActionRepository:
    global _action_repo
    if _action_repo is None:
        settings = get_settings()
        _action_repo = ValkeyActionRepository(settings.VALKEY_URL)
    return _action_repo


def get_event_repo() -> ValkeyEventRepository:
    global _event_repo
    if _event_repo is None:
        settings = get_settings()
        _event_repo = ValkeyEventRepository(settings.VALKEY_URL)
    return _event_repo


def get_user_repo() -> SqliteUserRepository:
    global _user_repo
    if _user_repo is None:
        settings = get_settings()
        _user_repo = SqliteUserRepository(db_path=settings.DB_PATH)
    return _user_repo


def get_chat_interactor() -> ChatInteractor:
    global _interactor
    if _interactor is None:
        _interactor = ChatInteractor(
            _get_tg_adapter(), get_action_repo(), get_event_repo()
        )
    return _interactor


def get_rule_service() -> RuleService:
    global _rule_service
    if _rule_service is None:
        settings = get_settings()
        rule_repo = SqliteRuleRepository(db_path=settings.DB_PATH)
        _rule_service = RuleService(
            rule_repo, get_action_repo(), _get_tg_adapter(), get_user_repo()
        )
    return _rule_service
