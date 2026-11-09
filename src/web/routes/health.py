import asyncio

from quart import Blueprint, current_app, jsonify

from src.container import _get_tg_adapter, get_action_repo, get_event_bus, get_user_repo
from src.infrastructure.tasks import run_with_task_deadline

health_bp = Blueprint("health", __name__)
HEALTH_CHECK_TIMEOUT = 2.0


async def _check_sqlite() -> bool:
    user_repo = get_user_repo()
    connect = getattr(user_repo, "_connect", None)
    if not connect:
        return False

    def _ping() -> bool:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return True

    try:
        return await run_with_task_deadline(
            lambda: asyncio.to_thread(_ping),
            asyncio.get_running_loop().time() + HEALTH_CHECK_TIMEOUT,
            name="health_sqlite",
        )
    except Exception:
        return False


async def _check_valkey() -> bool:
    action_repo = get_action_repo()
    redis = getattr(action_repo, "redis", None)
    if not redis:
        return False

    try:
        return bool(
            await run_with_task_deadline(
                redis.ping,
                asyncio.get_running_loop().time() + HEALTH_CHECK_TIMEOUT,
                name="health_valkey",
            )
        )
    except Exception:
        return False


async def _check_account(source_session: str | None) -> tuple[bool, bool]:
    try:
        user = await run_with_task_deadline(
            lambda: get_user_repo().get_user(1),
            asyncio.get_running_loop().time() + HEALTH_CHECK_TIMEOUT,
            name="health_account",
        )
        if user and user.is_authenticated():
            return True, user.session_string == source_session
        return False, False
    except Exception:
        return False, False


def _adapter_account_state(adapter) -> tuple[str | None, bool, bool]:
    return (
        adapter._source_session_string,
        bool(adapter._login_attempt_id),
        adapter._login_finalized,
    )


@health_bp.route("/api/live")
async def liveness():
    return jsonify({"status": "ok"})


@health_bp.route("/api/health")
async def health():
    adapter = _get_tg_adapter()
    account_state = _adapter_account_state(adapter)
    bus = get_event_bus()

    db_ok, valkey_ok, (account_configured, session_matches) = await asyncio.gather(
        _check_sqlite(), _check_valkey(), _check_account(account_state[0])
    )
    current_adapter = _get_tg_adapter()
    current_state = _adapter_account_state(current_adapter)
    if not current_adapter._retired and (
        current_adapter is not adapter or current_state != account_state
    ):
        # Login can change state on the same adapter; retry once rather than
        # combine an old saved-account result with the new session.
        adapter = current_adapter
        account_state = current_state
        account_configured, session_matches = await _check_account(account_state[0])
        current_adapter = _get_tg_adapter()
        current_state = _adapter_account_state(current_adapter)
    if (
        current_adapter is not adapter
        or current_adapter._retired
        or current_state != account_state
    ):
        # The account changed again, or teardown has not published a new one.
        account_configured = False
        session_matches = False
        adapter = current_adapter
        account_state = current_state

    client_exists = adapter.client is not None
    connected = bool(adapter.is_connected())
    _, has_login_attempt, login_finalized = account_state
    # This is cached state from the adapter's last successful authentication,
    # not a fresh Telegram RPC. Recovery belongs to the background worker.
    authorized = bool(
        account_configured
        and session_matches
        and connected
        and adapter._self_id
        and (not has_login_attempt or login_finalized)
    )

    queue_size = adapter._write_queue.queue_size()
    queue_running = adapter._write_queue.is_running()
    subscriber_count = len(bus._subscribers)

    sse_clients = current_app.sse.client_count

    healthy = (
        db_ok
        and valkey_ok
        and account_configured
        and client_exists
        and connected
        and authorized
        and queue_running
    )
    status = "ok" if healthy else "unhealthy"

    response = jsonify(
        {
            "status": status,
            "web": True,
            "db": db_ok,
            "cache": valkey_ok,
            "account_configured": account_configured,
            "telegram_client_exists": client_exists,
            "telegram_connected": connected,
            "telegram_authorized": authorized,
            "telegram_authenticated_call": None,
            "write_queue_depth": queue_size,
            "write_queue_running": bool(queue_running),
            "telegram_reconnect_count": int(
                getattr(adapter.client, "reconnect_count", 0)
            ),
            "event_bus_subscribers": subscriber_count,
            "sse_clients": sse_clients,
        }
    )
    response.status_code = 200 if healthy else 503
    return response
