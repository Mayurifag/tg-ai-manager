import asyncio

from src.domain.models import ActionLog
from src.domain.ports import ActionRepository
from src.infrastructure.logging import get_logger
from src.infrastructure.tasks import raise_if_task_stopped, run_with_task_deadline

logger = get_logger(__name__)
ACTION_LOG_TIMEOUT = 2.0


async def record_action(repository: ActionRepository, action: ActionLog) -> None:
    """Optional log storage must not indefinitely block Telegram writes or events."""
    try:
        deadline = asyncio.get_running_loop().time() + ACTION_LOG_TIMEOUT
        await run_with_task_deadline(
            lambda: repository.add_log(action), deadline, name="action_log_write"
        )
    except Exception as error:
        raise_if_task_stopped()
        logger.warning("action_log_unavailable", category=type(error).__name__)
