import traceback
from collections.abc import Awaitable, Callable

from src.domain.models import SystemEvent
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class EventBus:
    """Lightweight in-process pub/sub for SystemEvent.

    Subscribers are called in registration order. Rule engine must be
    registered before SSE so it can set event.is_read before rendering.
    Errors in one subscriber are logged but do not stop dispatch to the rest.
    """

    def __init__(self) -> None:
        self._subscribers: list[Callable[[SystemEvent], Awaitable[None]]] = []

    def subscribe(self, callback: Callable[[SystemEvent], Awaitable[None]]) -> None:
        """Register a subscriber. Callbacks are invoked in registration order."""
        self._subscribers.append(callback)

    async def dispatch(self, event: SystemEvent) -> None:
        """Dispatch event to all subscribers in order."""
        for cb in self._subscribers:
            try:
                await cb(event)
            except Exception as e:
                logger.error(
                    "event_bus_subscriber_error",
                    error=repr(e),
                    traceback=traceback.format_exc(),
                )
