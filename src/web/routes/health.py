from quart import Blueprint, jsonify
from quart import current_app

health_bp = Blueprint("health", __name__)


@health_bp.route("/health")
async def health():
    adapter = current_app.tg_adapter  # type: ignore[attr-defined]
    bus = current_app.event_bus  # type: ignore[attr-defined]

    connected = adapter.is_connected()
    queue_size = adapter._write_queue.queue_size()
    subscriber_count = len(bus._subscribers)

    from src.web.sse import connected_queues

    sse_clients = len(connected_queues)

    status = "ok" if connected else "degraded"

    return jsonify(
        {
            "status": status,
            "telegram_connected": connected,
            "write_queue_depth": queue_size,
            "event_bus_subscribers": subscriber_count,
            "sse_clients": sse_clients,
        }
    )
