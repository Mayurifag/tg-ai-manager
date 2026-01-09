import sqlite3
from src.config import get_settings
from src.adapters.telegram import TelethonAdapter
from src.adapters.valkey_repo import ValkeyActionRepository, ValkeyEventRepository
from src.rules.sqlite_repo import SqliteRuleRepository
from src.users.sqlite_repo import SqliteUserRepository
from src.application.interactors import ChatInteractor
from src.rules.service import RuleService
from src.infrastructure.security import CryptoManager

# Singleton instances
_tg_adapter: TelethonAdapter | None = None
_action_repo: ValkeyActionRepository | None = None
_event_repo: ValkeyEventRepository | None = None
_user_repo: SqliteUserRepository | None = None
_interactor: ChatInteractor | None = None
_rule_service: RuleService | None = None


def _get_tg_adapter() -> TelethonAdapter:
    global _tg_adapter
    # If adapter exists, return it.
    if _tg_adapter is not None:
        return _tg_adapter

    settings = get_settings()

    # 1. Fetch User Credentials from DB (Sync)
    api_id = None
    api_hash = None
    session_string = None
    crypto = CryptoManager()

    try:
        with sqlite3.connect(settings.DB_PATH) as conn:
            # We assume user ID 1 for single-tenant mode
            cur = conn.execute(
                "SELECT api_id, api_hash, session_string FROM users WHERE id = 1"
            )
            row = cur.fetchone()
            if row:
                if row[0]:
                    api_id = row[0]
                if row[1]:
                    # Decrypt API Hash
                    api_hash = crypto.decrypt(row[1])
                # Decrypt Session String
                session_string = crypto.decrypt(row[2])
    except Exception as e:
        print(f"Error fetching credentials for adapter: {e}")

    # 2. Initialize Adapter
    # TelethonAdapter now gracefully handles None for api_id/hash
    _tg_adapter = TelethonAdapter(session_string, api_id, api_hash)
    return _tg_adapter


def reload_tg_adapter(api_id: int, api_hash: str, session_string: str = None):
    """
    Force re-initialization of the adapter (e.g. after login).
    """
    global _tg_adapter
    if _tg_adapter:
        # Disconnect old one silently
        try:
            # We can't await here easily in sync container,
            # but usually this called from async context where we can manage it.
            # Actually, we'll just replace the reference.
            pass
        except Exception:
            pass

    _tg_adapter = TelethonAdapter(session_string, api_id, api_hash)

    # Re-inject into interactor if it exists
    global _interactor
    if _interactor:
        _interactor.repository = _tg_adapter

    # Re-inject into rule service
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
