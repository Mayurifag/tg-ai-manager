import asyncio
from telethon.errors import FloodWaitError
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


async def mark_as_read_job(
    ctx, chat_id: int, topic_id: int | None = None, max_id: int | None = None
):
    # Import inside function to avoid circular dependency
    from src.container import _get_tg_adapter

    adapter = _get_tg_adapter()
    if not adapter or not adapter.is_connected():
        logger.warning("job_skipped_not_connected", job="mark_as_read")
        return

    try:
        await adapter.mark_as_read(chat_id, topic_id, max_id)
    except FloodWaitError as e:
        logger.warning("flood_wait", seconds=e.seconds)
        # Sleep inside the task to throttle this specific worker slot
        await asyncio.sleep(e.seconds)
        raise e


async def send_reaction_job(ctx, chat_id: int, msg_id: int, emoji: str):
    from src.container import _get_tg_adapter

    adapter = _get_tg_adapter()
    if not adapter or not adapter.is_connected():
        return

    try:
        await adapter.send_reaction(chat_id, msg_id, emoji)
    except FloodWaitError as e:
        logger.warning("flood_wait", seconds=e.seconds)
        await asyncio.sleep(e.seconds)
        raise e


# --- Startup/Shutdown Hooks for the Worker ---


async def startup(ctx):
    logger.info("arq_worker_startup")


async def shutdown(ctx):
    logger.info("arq_worker_shutdown")


# We define the WorkerSettings class structure here
class WorkerSettings:
    functions = [mark_as_read_job, send_reaction_job]
    on_startup = startup
    on_shutdown = shutdown
    # Retry policy: Backoff
    max_tries = 5
    # Retry delay: 5s, 10s, 20s...
    retry_jobs = True