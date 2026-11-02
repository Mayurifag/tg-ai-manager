"""App-scoped service accessors."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, cast

from quart import current_app

from src.adapters.telegram import TelethonAdapter
from src.application.interactors import ChatInteractor
from src.config import get_settings
from src.domain.ports import ActionRepository, EventRepository
from src.infrastructure.event_bus import EventBus
from src.domain.models import SystemEvent
from src.infrastructure.logging import get_logger
from src.rules.service import RuleService
from src.users.ports import UserRepository

if TYPE_CHECKING:
    from src.web.types import TypedQuart

logger = get_logger(__name__)


def _app() -> TypedQuart:
    return cast("TypedQuart", current_app._get_current_object())  # noqa: SLF001


def _get_tg_adapter() -> TelethonAdapter:
    return _app().tg_adapter


def get_chat_interactor() -> ChatInteractor:
    return _app().chat_interactor


def get_rule_service() -> RuleService:
    return _app().rule_service


def get_action_repo() -> ActionRepository:
    return _app().action_repo


def get_event_repo() -> EventRepository:
    return _app().event_repo


def get_user_repo() -> UserRepository:
    return _app().user_repo


def get_event_bus() -> EventBus:
    return _app().event_bus


def bind_adapter_events(app: TypedQuart, adapter: TelethonAdapter) -> None:
    """Keep an old adapter's delayed events out of a replacement account."""

    async def dispatch(event: SystemEvent) -> None:
        await app.event_bus.dispatch(
            event,
            is_current=lambda: app.tg_adapter is adapter and not adapter._retired,
        )

    adapter.add_event_listener(dispatch)


async def reload_tg_adapter(session_string: Optional[str] = None) -> None:
    settings = get_settings()
    new_adapter = TelethonAdapter(
        session_string=session_string,
        api_id=settings.TG_API_ID,
        api_hash=settings.TG_API_HASH,
    )

    app = _app()
    old_adapter = app.tg_adapter
    bind_adapter_events(app, new_adapter)
    old_adapter._retired = True
    app.sse.invalidate_clients()
    await app.rule_service.retire_account()
    await old_adapter.disconnect()
    app.tg_adapter = new_adapter
    app.chat_interactor.replace_repository(new_adapter)
    app.rule_service.chat_repo = new_adapter
