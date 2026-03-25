import asyncio

from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


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
