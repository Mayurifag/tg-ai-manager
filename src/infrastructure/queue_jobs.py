import time

from arq.connections import ArqRedis
from arq.worker import Retry
from telethon.errors import FloodWaitError

from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

FLOOD_LOCK_KEY = "global:telegram_flood_wait"


async def _check_flood_lock(ctx: dict) -> None:
    """
    Checks if a global flood wait is active.
    If active, raises Retry to defer the job.
    """
    redis: ArqRedis = ctx["redis"]
    lock_timestamp = await redis.get(FLOOD_LOCK_KEY)

    if lock_timestamp:
        try:
            wait_until = float(lock_timestamp)
            now = time.time()
            if now < wait_until:
                delay = wait_until - now
                logger.info("job_deferred_flood_lock", delay=delay)
                # Retry after the lock expires + buffer
                raise Retry(defer=delay + 1)
        except ValueError:
            pass


async def _handle_flood_wait(ctx: dict, e: FloodWaitError) -> None:
    """
    Sets the global flood lock and retries the job.
    """
    redis: ArqRedis = ctx["redis"]
    wait_seconds = e.seconds

    # Set global lock
    expire_at = time.time() + wait_seconds
    # Set key with expiration just to be safe (auto-cleanup)
    await redis.set(FLOOD_LOCK_KEY, str(expire_at), ex=wait_seconds + 5)

    logger.warning("flood_wait_triggered", wait_seconds=wait_seconds)

    # Retry this specific job
    raise Retry(defer=wait_seconds + 1)


async def mark_as_read_job(
    ctx, chat_id: int, topic_id: int | None = None, max_id: int | None = None
):
    await _check_flood_lock(ctx)

    # Import inside function to avoid circular dependency
    from src.container import _get_tg_adapter

    adapter = _get_tg_adapter()
    if not adapter or not adapter.is_connected():
        # If not connected, we might retry or just drop.
        # For now, let's retry with a fixed delay hoping connection returns.
        raise Retry(defer=10)

    try:
        await adapter.mark_as_read(chat_id, topic_id, max_id)
    except FloodWaitError as e:
        await _handle_flood_wait(ctx, e)
    except Exception as e:
        logger.error("mark_as_read_failed", error=str(e))
        raise


async def send_reaction_job(ctx, chat_id: int, msg_id: int, emoji: str):
    await _check_flood_lock(ctx)

    from src.container import _get_tg_adapter

    adapter = _get_tg_adapter()
    if not adapter or not adapter.is_connected():
        raise Retry(defer=10)

    try:
        await adapter.send_reaction(chat_id, msg_id, emoji)
    except FloodWaitError as e:
        await _handle_flood_wait(ctx, e)
    except Exception as e:
        logger.error("send_reaction_failed", error=str(e))
        raise


# --- Hooks ---


async def startup(ctx):
    logger.info("arq_worker_startup")


async def shutdown(ctx):
    logger.info("arq_worker_shutdown")


async def on_job_completion(ctx: dict) -> None:
    """
    Hook called when a job finishes (success or failure).
    We use this to log permanently failed jobs (Dead Letters).
    """
    job_id = ctx.get("job_id")
    success = ctx.get("success", False)

    if success:
        return

    # Arq context doesn't explicitly explicitly expose "retries_left" in this hook easily
    # for the *current* execution, but if success is False and we are here,
    # it *might* be a retry or a final failure.
    # Actually, Arq calls on_job_completion after *every* attempt.
    # To detect final failure, we check result logic or retry limits?
    # Arq doesn't make "is_final_failure" obvious in this hook.

    # However, ctx['job_try'] tells us the attempt number.
    # If job_try >= max_tries, it's a dead letter.

    job_try = ctx.get("job_try", 0)
    settings_cls = ctx.get("settings", WorkerSettings)
    max_tries = settings_cls.max_tries

    if job_try >= max_tries:
        # Fetch the exception info if available
        # Arq stores result/error in database, but here we might just log generic failure
        # For simplicity, we assume if we reached max_tries, it failed.

        # Import here to avoid circular dependency
        from src.container import get_queue_monitor

        monitor = get_queue_monitor()
        # args are in ctx['args'] and ctx['kwargs']
        function_name = "unknown"
        # We can't easily get the function name string from ctx['function'] directly if it's a coroutine wrapper
        # but we can try.

        err_msg = "Max retries reached"

        await monitor.log_dead_letter(
            job_id=job_id,
            function=function_name,
            args=str(ctx.get("args")),
            error=err_msg,
        )


class WorkerSettings:
    functions = [mark_as_read_job, send_reaction_job]
    on_startup = startup
    on_shutdown = shutdown
    on_job_completion = on_job_completion
    # Retry policy: Backoff
    max_tries = 5
    # Retry delay: 5s, 10s, 20s... (Managed by functions explicitly or default)
    retry_jobs = True
