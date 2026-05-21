import asyncio

from quart import Blueprint, jsonify

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


async def _check_telegram(adapter) -> tuple[bool, bool, bool, bool]:
    client_exists = adapter.client is not None
    connected = adapter.is_connected()
    authorized = False
    authenticated_call = False

    if client_exists and connected:
        try:
            authorized = bool(
                await asyncio.wait_for(adapter.client.is_user_authorized(), timeout=2.0)
            )
            authenticated_call = bool(
                await asyncio.wait_for(adapter.client.get_me(), timeout=2.0)
            )
        except Exception:
            authorized = False
            authenticated_call = False

    return client_exists, connected, authorized, authenticated_call


@health_bp.route("/api/health")
async def health():
    adapter = _get_tg_adapter()
    bus = get_event_bus()

    db_ok, valkey_ok = await asyncio.gather(_check_sqlite(), _check_valkey())
    client_exists, connected, authorized, authenticated_call = await _check_telegram(
        adapter
    )

    queue_size = adapter._write_queue.queue_size()
    subscriber_count = len(bus._subscribers)

    from src.web.sse import connected_queues

    sse_clients = len(connected_queues)

    healthy = (
        db_ok
        and valkey_ok
        and client_exists
        and connected
        and authorized
        and authenticated_call
    )
    status = "ok" if healthy else "unhealthy"

    response = jsonify(
        {
            "status": status,
            "web": True,
            "db": db_ok,
            "cache": valkey_ok,
            "telegram_client_exists": client_exists,
            "telegram_connected": connected,
            "telegram_authorized": authorized,
            "telegram_authenticated_call": authenticated_call,
            "write_queue_depth": queue_size,
            "event_bus_subscribers": subscriber_count,
            "sse_clients": sse_clients,
        }
    )
    response.status_code = 200 if healthy else 503
    return response
