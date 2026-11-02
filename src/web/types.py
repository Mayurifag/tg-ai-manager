from quart import Quart
import asyncio

from src.adapters.telegram.client import TelethonAdapter
from src.application.interactors import ChatInteractor
from src.domain.ports import ActionRepository, EventRepository
from src.infrastructure.event_bus import EventBus
from src.infrastructure.tasks import BackgroundTasks
from src.rules.service import RuleService
from src.users.ports import UserRepository
from src.web.sse import EventBroadcaster


class TypedQuart(Quart):
    tg_adapter: TelethonAdapter
    action_repo: ActionRepository
    event_repo: EventRepository
    user_repo: UserRepository
    rule_service: RuleService
    chat_interactor: ChatInteractor
    event_bus: EventBus
    managed_tasks: BackgroundTasks
    login_lock: asyncio.Lock
    sse: EventBroadcaster
    shutdown_event: asyncio.Event
