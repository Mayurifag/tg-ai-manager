import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar
from weakref import ReferenceType, ref

T = TypeVar("T")
U = TypeVar("U")


def raise_if_cancelled() -> None:
    """Do not continue a task whose cancellation was suppressed downstream."""
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


_task_deadline: ContextVar[
    tuple[
        ReferenceType[asyncio.Task[Any]],
        float,
        tuple[tuple[ReferenceType[asyncio.Task[Any]], int], ...],
    ]
    | None
] = ContextVar("task_deadline", default=None)


@contextmanager
def task_deadline(
    deadline: float,
    *,
    cancel_with: asyncio.Task[Any] | None = None,
    cancellation_baseline: int = 0,
) -> Iterator[None]:
    """Limit this task and explicitly owned children, not independent tasks."""
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("A deadline requires a task")
    current = _task_deadline.get()
    owners: tuple[tuple[ReferenceType[asyncio.Task[Any]], int], ...] = ()
    if current is not None and (
        current[0]() is task
        or (cancel_with is not None and current[0]() is cancel_with)
    ):
        deadline = min(deadline, current[1])
        owners = current[2]
    if cancel_with is not None:
        owners += ((ref(cancel_with), cancellation_baseline),)
    token = _task_deadline.set((ref(task), deadline, owners))
    try:
        yield
    finally:
        _task_deadline.reset(token)


def raise_if_task_stopped() -> None:
    raise_if_cancelled()
    current = _task_deadline.get()
    # Child tasks inherit context, but their independent work has its own owner.
    if current is not None and current[0]() is asyncio.current_task():
        # The worker can be cancelled before it gets a turn to cancel this task.
        for owner_ref, cancellation_baseline in current[2]:
            owner = owner_ref()
            if owner is not None and owner.cancelling() > cancellation_baseline:
                raise asyncio.CancelledError
        if asyncio.get_running_loop().time() >= current[1]:
            raise TimeoutError


async def run_with_task_deadline(
    operation: Callable[[], Awaitable[T]],
    deadline: float | None,
    *,
    name: str,
    allow_pending_cancellation: bool = False,
) -> T:
    """Join owned work; None means no deadline apart from an enclosing one."""
    loop = asyncio.get_running_loop()
    owner = asyncio.current_task()
    current = _task_deadline.get()
    if current is not None and current[0]() is owner:
        deadline = current[1] if deadline is None else min(deadline, current[1])
    baseline = owner.cancelling() if owner and allow_pending_cancellation else 0

    def check_owner() -> None:
        if owner is not None and owner.cancelling() > baseline:
            raise asyncio.CancelledError

    check_owner()

    async def run() -> T:
        with task_deadline(
            deadline if deadline is not None else float("inf"),
            cancel_with=owner,
            cancellation_baseline=baseline,
        ):
            raise_if_task_stopped()
            result = await operation()
            raise_if_task_stopped()
            return result

    task = asyncio.create_task(run(), name=name)
    try:
        timeout = None if deadline is None else max(0, deadline - loop.time())
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                check_owner()
            except Exception:
                check_owner()
            check_owner()
            raise TimeoutError
        result = await task
        check_owner()
        if deadline is not None and loop.time() >= deadline:
            raise TimeoutError
        return result
    except BaseException:
        if not task.done():
            task.cancel()
            # Keep ownership even if the caller is cancelled again while joining.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        if task.done() and not task.cancelled():
            task.exception()
        raise


async def gather_owned(
    first: Callable[[], Awaitable[T]], second: Callable[[], Awaitable[U]]
) -> tuple[T, U]:
    """Run two related operations concurrently and join both on failure."""
    raise_if_task_stopped()
    owner = asyncio.current_task()

    async def run(operation: Callable[[], Awaitable[T]]) -> T:
        with task_deadline(float("inf"), cancel_with=owner):
            raise_if_task_stopped()
            result = await operation()
            raise_if_task_stopped()
            return result

    tasks = (asyncio.create_task(run(first)), asyncio.create_task(run(second)))
    try:
        first_result, second_result = await asyncio.gather(*tasks)
        return first_result, second_result
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        joined = asyncio.gather(*tasks, return_exceptions=True)
        while not joined.done():
            try:
                await asyncio.shield(joined)
            except asyncio.CancelledError:
                continue
        joined.result()
        raise_if_task_stopped()


class BackgroundTasks:
    def __init__(self, logger: Any) -> None:
        self._logger = logger
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    def create(self, awaitable: Awaitable[Any], name: str) -> asyncio.Task[Any]:
        if self._closed:
            # Callers construct coroutines before handing them to the manager.
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise RuntimeError("Background task manager is closed")
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
                category=type(exc).__name__,
            )

    async def shutdown(self, timeout: float = 3.0) -> None:
        self._closed = True
        if not self._tasks:
            return
        for task in list(self._tasks):
            task.cancel()
        _, pending = await asyncio.wait(self._tasks, timeout=timeout)
        if pending:
            self._logger.error(
                "background_tasks_shutdown_timed_out",
                task_names=sorted(task.get_name() for task in pending),
            )
