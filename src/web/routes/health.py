from quart import Blueprint, jsonify
from src.container import get_queue_monitor, _get_tg_adapter

health_bp = Blueprint("health", __name__)


@health_bp.route("/health", methods=["GET"])
async def health_check():
    adapter = _get_tg_adapter()
    monitor = get_queue_monitor()

    tg_connected = adapter.is_connected() if adapter else False

    # Check Valkey
    valkey_status = False
    try:
        await monitor.redis.ping()
        valkey_status = True
    except Exception:
        pass

    failed_jobs = await monitor.get_failed_jobs(limit=1)

    status = "healthy"
    if not valkey_status:
        status = "critical"
    elif not tg_connected:
        status = "degraded"

    return jsonify(
        {
            "status": status,
            "telegram_connected": tg_connected,
            "valkey_connected": valkey_status,
            "has_failed_jobs": len(failed_jobs) > 0,
        }
    )
