import asyncio
from collections.abc import Awaitable
from typing import Any


class BackgroundTasks:
    def __init__(self, logger: Any) -> None:
        self._logger = logger
        self._tasks: set[asyncio.Task[Any]] = set()

    def create(self, awaitable: Awaitable[Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(awaitable, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc:
            self._logger.error(
                "background_task_failed",
                task_name=task.get_name(),
                error=repr(exc),
            )

    async def shutdown(self, timeout: float = 3.0) -> None:
        if not self._tasks:
            return
        for task in list(self._tasks):
            task.cancel()
        await asyncio.wait(self._tasks, timeout=timeout)
