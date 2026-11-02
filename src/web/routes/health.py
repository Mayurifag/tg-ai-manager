import asyncio

from quart import Blueprint, current_app, jsonify

from src.container import _get_tg_adapter, get_action_repo, get_event_bus, get_user_repo

health_bp = Blueprint("health", __name__)


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
        return await asyncio.wait_for(asyncio.to_thread(_ping), timeout=2.0)
    except Exception:
        return False


async def _check_valkey() -> bool:
    action_repo = get_action_repo()
    redis = getattr(action_repo, "redis", None)
    if not redis:
        return False

    try:
        return bool(await asyncio.wait_for(redis.ping(), timeout=2.0))
    except Exception:
        return False


async def _check_account() -> bool:
    try:
        user = await asyncio.wait_for(get_user_repo().get_user(1), timeout=2.0)
        return bool(user and user.is_authenticated())
    except Exception:
        return False


@health_bp.route("/api/live")
async def liveness():
    return jsonify({"status": "ok"})


@health_bp.route("/api/health")
async def health():
    adapter = _get_tg_adapter()
    bus = get_event_bus()

    db_ok, valkey_ok, account_configured = await asyncio.gather(
        _check_sqlite(), _check_valkey(), _check_account()
    )
    client_exists = adapter.client is not None
    connected = bool(adapter.is_connected())
    # This is cached state from the adapter's last successful authentication,
    # not a fresh Telegram RPC. Recovery belongs to the background worker.
    authorized = bool(account_configured and connected and adapter._self_id)

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
