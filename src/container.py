"""
App-scoped service accessors.

All services are created in src/web/__init__.py:startup (before_serving) and
attached to the Quart app object. These functions are thin delegates to
current_app — they work in any request context or app context.
"""

from quart import current_app

from src.adapters.telegram import TelethonAdapter
from src.application.interactors import ChatInteractor
from src.config import get_settings
from src.infrastructure.logging import get_logger
from src.rules.service import RuleService

logger = get_logger(__name__)


def _get_tg_adapter() -> TelethonAdapter:
    return current_app.tg_adapter  # type: ignore[attr-defined]


def get_chat_interactor() -> ChatInteractor:
    return current_app.chat_interactor  # type: ignore[attr-defined]


def get_rule_service() -> RuleService:
    return current_app.rule_service  # type: ignore[attr-defined]


def get_action_repo():
    return current_app.action_repo  # type: ignore[attr-defined]


def get_event_repo():
    return current_app.event_repo  # type: ignore[attr-defined]


def get_user_repo():
    return current_app.user_repo  # type: ignore[attr-defined]


def reload_tg_adapter(
    session_string: str = None,  # type: ignore[assignment]
) -> None:
    """Hot-swap the Telegram adapter (called on QR login start).

    Creates a new adapter, re-wires it to the event bus, and updates all
    app-scoped references that hold a pointer to the old adapter.
    """
    settings = get_settings()
    new_adapter = TelethonAdapter(
        session_string=session_string,
        api_id=settings.TG_API_ID,
        api_hash=settings.TG_API_HASH,
    )

    # Re-wire: event bus dispatch is the sole listener
    new_adapter.add_event_listener(current_app.event_bus.dispatch)  # type: ignore[attr-defined]

    # Update app-scoped references
    current_app.tg_adapter = new_adapter  # type: ignore[attr-defined]
    current_app.chat_interactor.repository = new_adapter  # type: ignore[attr-defined]
    current_app.rule_service.chat_repo = new_adapter  # type: ignore[attr-defined]
