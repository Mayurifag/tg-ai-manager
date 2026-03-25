from quart import Quart

from src.adapters.telegram.client import TelethonAdapter
from src.application.interactors import ChatInteractor
from src.domain.ports import ActionRepository, EventRepository
from src.infrastructure.event_bus import EventBus
from src.rules.service import RuleService
from src.users.ports import UserRepository


class TypedQuart(Quart):
    tg_adapter: TelethonAdapter
    action_repo: ActionRepository
    event_repo: EventRepository
    user_repo: UserRepository
    rule_service: RuleService
    chat_interactor: ChatInteractor
    event_bus: EventBus
