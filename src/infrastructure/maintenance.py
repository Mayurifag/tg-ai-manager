import asyncio

from telethon.errors import FloodWaitError

from src.infrastructure.logging import get_logger

logger = get_logger(__name__)
TELEGRAM_RECOVERY_INTERVAL = 5


async def job_telegram_recovery(rule_service, user_repo, shutdown_event):
    """Reconnect promptly and reconcile approved scopes every 5 seconds.

    This is a safety net, not the primary update transport. Never infer read
    permission from a mention, reaction, or a partial global text rule.
    """
    delay = TELEGRAM_RECOVERY_INTERVAL
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
            break
        except asyncio.TimeoutError:
            pass

        delay = TELEGRAM_RECOVERY_INTERVAL
        try:
            user = await user_repo.get_user(1)
            if not user or not user.is_authenticated():
                continue
            adapter = rule_service.chat_repo
            if not adapter.is_connected():
                await asyncio.wait_for(adapter.connect(), timeout=20)
            if adapter.is_connected():
                await asyncio.wait_for(rule_service.reconcile_autoread(), timeout=20)
        except FloodWaitError as e:
            delay = max(TELEGRAM_RECOVERY_INTERVAL, e.seconds)
            logger.warning("telegram_recovery_flood_wait", seconds=e.seconds)
        except Exception as e:
            logger.warning("telegram_recovery_failed", error=repr(e))


async def job_background_maintenance(
    action_repo,
    event_repo,
    interactor,
    user_repo,
    shutdown_event,
):
    """Background task to clean old logs, enforce cache limits, and refresh premium status."""
    logger.info("maintenance_job_started")
    while not shutdown_event.is_set():
        try:
            await action_repo.cleanup_expired()
            await event_repo.cleanup_expired()

            user = await user_repo.get_user(1)
            if user and user.is_authenticated():
                await interactor.run_storage_maintenance()

                is_premium = await interactor.get_self_premium_status()
                if user.is_premium != is_premium:
                    user.is_premium = is_premium
                    await user_repo.save_user(user)
                    logger.info("premium_status_updated", status=is_premium)
            else:
                logger.info("maintenance_skipped_no_authenticated_user")

        except Exception as e:
            logger.error("maintenance_job_error", error=str(e))

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass
