from quart import Quart
import asyncio

from src.adapters.telegram.client import TelethonAdapter
from src.application.interactors import ChatInteractor
from src.domain.ports import ActionRepository, EventRepository
from src.infrastructure.event_bus import EventBus
from src.infrastructure.tasks import BackgroundTasks
from src.rules.service import RuleService
from src.users.ports import UserRepository
from src.web.requests import ValidatedJSONProvider, ValidatedRequest
from src.web.sse import EventBroadcaster


class TypedQuart(Quart):
    json_provider_class = ValidatedJSONProvider
    request_class = ValidatedRequest

    async def startup(self) -> None:
        owner = asyncio.current_task()
        owns_startup = False
        try:
            if self._shutdown_started:
                self.shutdown_event.set()
                raise RuntimeError("Application is shutting down")
            if self._startup_owner is not None or self._startup_complete:
                raise RuntimeError(
                    "Application startup already in progress or complete"
                )
            self._startup_owner = owner
            owns_startup = True
            await super().startup()
        except Exception:
            # Quart forwards lifespan failure text to the server, whose process
            # traceback can otherwise include chained provider exceptions.
            raise RuntimeError("Application startup failed") from None
        finally:
            if owns_startup and self._startup_owner is owner:
                self._startup_owner = None

    async def shutdown(self) -> None:
        inflight = self._shutdown_inflight
        if inflight is not None:
            # A concurrent caller joins the existing pass without cancelling
            # its owner, even if this waiter is itself cancelled repeatedly.
            cancelled = False
            while not inflight.done():
                try:
                    await asyncio.shield(inflight)
                except asyncio.CancelledError:
                    cancelled = True
            outcome = inflight.result()
            if cancelled or outcome == "cancelled":
                raise asyncio.CancelledError
            if outcome != "complete":
                raise RuntimeError("Application shutdown failed") from None
            return

        inflight = asyncio.get_running_loop().create_future()
        self._shutdown_inflight = inflight
        outcome = "failed"
        try:
            self._shutdown_started = True
            self.shutdown_event.set()
            owner = self._startup_owner
            if (
                owner is not None
                and owner is not asyncio.current_task()
                and not owner.done()
                and not owner.cancelling()
                and not self._startup_unwinding
            ):
                owner.cancel()
            try:
                await super().shutdown()
            except Exception:
                raise RuntimeError("Application shutdown failed") from None
            outcome = "complete"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            inflight.set_result(outcome)
            self._shutdown_inflight = None

    tg_adapter: TelethonAdapter
    action_repo: ActionRepository
    event_repo: EventRepository
    user_repo: UserRepository
    rule_service: RuleService
    chat_interactor: ChatInteractor
    event_bus: EventBus
    managed_tasks: BackgroundTasks
    login_lock: asyncio.Lock
    _account_transition_revision: int
    _account_transition_depth: int
    _lifecycle_lock: asyncio.Lock
    _startup_owner: asyncio.Task[None] | None
    _startup_complete: bool
    _startup_unwinding: bool
    _shutdown_started: bool
    _shutdown_inflight: asyncio.Future[str] | None
    sse: EventBroadcaster
    shutdown_event: asyncio.Event
