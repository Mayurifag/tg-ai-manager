import asyncio
import traceback
from typing import Awaitable, Callable, List, TypeVar

from src.domain.models import SystemEvent
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class EventBus:
    def __init__(self):
        self._subscribers: List[Callable[[SystemEvent], Awaitable[None]]] = []

    def subscribe(self, callback: Callable[[SystemEvent], Awaitable[None]]):
        """Register a new subscriber."""
        self._subscribers.append(callback)

    async def publish(self, event: SystemEvent):
        """Publish an event to all subscribers."""
        if not self._subscribers:
            return

        # Execute all subscribers concurrently
        tasks = []
        for sub in self._subscribers:
            tasks.append(asyncio.create_task(self._safe_execute(sub, event)))

        await asyncio.gather(*tasks)

    async def _safe_execute(self, sub, event):
        try:
            await sub(event)
        except Exception as e:
            logger.error(
                "event_bus_subscriber_error",
                error=repr(e),
                traceback=traceback.format_exc(),
            )
