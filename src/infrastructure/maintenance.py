import asyncio

from telethon.errors import FloodWaitError

from src.infrastructure.logging import get_logger
from src.infrastructure.tasks import raise_if_task_stopped, run_with_task_deadline

logger = get_logger(__name__)
TELEGRAM_RECOVERY_INTERVAL = 10
# shortcut: allow 20 seconds per recovery connection; revisit if normal
# reconnections routinely need longer.
TELEGRAM_RECOVERY_CONNECT_TIMEOUT = 20


def _is_saved_account(adapter, current, session, shutdown_event):
    return (
        not shutdown_event.is_set()
        and current is adapter
        and not adapter._retired
        and adapter._source_session_string == session
    )


async def _reconcile_saved_account(rule_service, adapter, session, shutdown_event):
    # The owned recovery task starts separately, so ownership can change
    # between the recovery loop's check and the start of reconciliation.
    if _is_saved_account(adapter, rule_service.chat_repo, session, shutdown_event):
        await rule_service.reconcile_autoread(adapter)


async def _connect_saved_account(rule_service, adapter, session, shutdown_event):
    def is_current() -> bool:
        return _is_saved_account(
            adapter, rule_service.chat_repo, session, shutdown_event
        )

    try:
        raise_if_task_stopped()
        if is_current():
            await adapter.connect(is_current=is_current)
    finally:
        raise_if_task_stopped()


async def job_telegram_recovery(rule_service, user_repo, shutdown_event):
    """Reconnect promptly and reconcile approved scopes every 10 seconds.

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
            user = await run_with_task_deadline(
                lambda: user_repo.get_user(1), None, name="recovery_saved_user"
            )
            if not user or not user.is_authenticated():
                continue
            adapter = rule_service.chat_repo
            if not _is_saved_account(
                adapter, rule_service.chat_repo, user.session_string, shutdown_event
            ):
                # A fresh QR attempt is not the saved account, even if it is
                # already authorized by Telegram but not yet finalized.
                continue
            if not adapter.is_connected():
                deadline = (
                    asyncio.get_running_loop().time()
                    + TELEGRAM_RECOVERY_CONNECT_TIMEOUT
                )
                await run_with_task_deadline(
                    lambda: _connect_saved_account(
                        rule_service, adapter, user.session_string, shutdown_event
                    ),
                    deadline,
                    name="recovery_connect",
                )
            if (
                _is_saved_account(
                    adapter, rule_service.chat_repo, user.session_string, shutdown_event
                )
                and adapter.is_connected()
            ):
                session = user.session_string
                deadline = asyncio.get_running_loop().time() + 20
                await run_with_task_deadline(
                    lambda: _reconcile_saved_account(
                        rule_service, adapter, session, shutdown_event
                    ),
                    deadline,
                    name="recovery_reconciliation",
                )
        except FloodWaitError as e:
            raise_if_task_stopped()
            delay = max(TELEGRAM_RECOVERY_INTERVAL, e.seconds)
            logger.warning("telegram_recovery_flood_wait", seconds=e.seconds)
        except Exception as e:
            raise_if_task_stopped()
            logger.warning("telegram_recovery_failed", category=type(e).__name__)


async def job_background_maintenance(
    action_repo,
    event_repo,
    interactor,
    user_repo,
    shutdown_event,
):
    """Clean old logs and refresh premium status."""

    async def run_once() -> None:
        try:
            raise_if_task_stopped()
            await action_repo.cleanup_expired()
            raise_if_task_stopped()
            await event_repo.cleanup_expired()
            raise_if_task_stopped()

            user = await user_repo.get_user(1)
            raise_if_task_stopped()
            if user and user.is_authenticated():
                adapter = interactor.repository

                def is_current():
                    raise_if_task_stopped()
                    return _is_saved_account(
                        adapter,
                        interactor.repository,
                        user.session_string,
                        shutdown_event,
                    )

                if is_current():
                    is_premium = await adapter.get_self_premium_status()
                    raise_if_task_stopped()
                    if user.is_premium != is_premium and is_current():
                        updated = await user_repo.update_fields(
                            user.id,
                            {"is_premium": is_premium},
                            expected_session=user.session_string,
                        )
                        raise_if_task_stopped()
                        if updated:
                            logger.info("premium_status_updated", status=is_premium)
            else:
                logger.info("maintenance_skipped_no_authenticated_user")
        finally:
            raise_if_task_stopped()

    logger.info("maintenance_job_started")
    while not shutdown_event.is_set():
        try:
            await run_with_task_deadline(
                run_once, None, name="background_maintenance_pass"
            )
        except Exception as e:
            raise_if_task_stopped()
            logger.error("maintenance_job_error", category=type(e).__name__)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass
