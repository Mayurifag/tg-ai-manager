from collections.abc import Awaitable, Callable

from src.domain.models import SystemEvent
from src.infrastructure.logging import get_logger
from src.infrastructure.tasks import raise_if_task_stopped, run_with_task_deadline

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
        """Dispatch in order, stopping on cancellation or account retirement."""

        async def deliver() -> None:
            raise_if_task_stopped()
            for cb in self._subscribers:
                raise_if_task_stopped()
                if is_current is not None and not is_current():
                    return
                raise_if_task_stopped()
                try:
                    await cb(event)
                except Exception as e:
                    raise_if_task_stopped()
                    logger.error(
                        "event_bus_subscriber_error",
                        subscriber=getattr(cb, "__qualname__", type(cb).__name__),
                        category=type(e).__name__,
                    )
                finally:
                    # Subscribers may suppress cancellation or replace it with an error.
                    raise_if_task_stopped()

        await run_with_task_deadline(deliver, None, name="event_bus_dispatch")
