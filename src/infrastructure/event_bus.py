import traceback
from collections.abc import Awaitable, Callable

from src.domain.models import SystemEvent
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class EventBus:
    """Lightweight in-process pub/sub for SystemEvent.

    Subscribers are called in registration order.
    Errors in one subscriber are logged but do not stop dispatch to the rest.
    """

    def __init__(self) -> None:
        self._subscribers: list[Callable[[SystemEvent], Awaitable[None]]] = []

    def subscribe(self, callback: Callable[[SystemEvent], Awaitable[None]]) -> None:
        """Register a subscriber. Callbacks are invoked in registration order."""
        self._subscribers.append(callback)

    async def dispatch(
        self, event: SystemEvent, *, is_current: Callable[[], bool] | None = None
    ) -> None:
        """Dispatch in order, stopping if the source account was retired."""
        for cb in self._subscribers:
            if is_current is not None and not is_current():
                return
            try:
                await cb(event)
            except Exception as e:
                logger.error(
                    "event_bus_subscriber_error",
                    error=repr(e),
                    traceback=traceback.format_exc(),
                )
