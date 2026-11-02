import asyncio

from src.domain.models import ActionLog
from src.domain.ports import ActionRepository
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)
ACTION_LOG_TIMEOUT = 2.0


async def record_action(repository: ActionRepository, action: ActionLog) -> None:
    """Optional log storage must not indefinitely block Telegram writes or events."""
    try:
        await asyncio.wait_for(repository.add_log(action), timeout=ACTION_LOG_TIMEOUT)
    except Exception as error:
        logger.warning("action_log_unavailable", category=type(error).__name__)
