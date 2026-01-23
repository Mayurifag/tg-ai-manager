from quart import Blueprint, jsonify, render_template, request

from src.container import get_queue_monitor

queue_bp = Blueprint("queue", __name__)


@queue_bp.route("/api/queue/failed", methods=["GET"])
async def get_failed_jobs():
    monitor = get_queue_monitor()
    jobs = await monitor.get_failed_jobs()

    # Return HTML partial if requested by HTMX
    if request.headers.get("HX-Request"):
        return await render_template("partials/queue_status.html.j2", failed_jobs=jobs)

    return jsonify({"jobs": jobs})


@queue_bp.route("/api/queue/retry/<job_id>", methods=["POST"])
async def retry_job(job_id: str):
    """
    Retrying a dead letter involves re-enqueueing the original function with original args.
    Note: 'job_id' here refers to the ID in the Dead Letter Log, not Arq's internal ID.
    """
    # monitor = get_queue_monitor()
    # queue_service = get_queue_service()

    # Fetch job details from monitor
    # In a real impl, we'd need a way to look up specific failed job details.
    # Current monitor implementation returns a list.
    # For MVP, we pass args via body or assume the frontend sends context.
    # Since QueueMonitor uses ZSET, searching by ID is O(N) or requires client to send payload.

    # Simplified: Frontend sends the payload needed to retry
    data = await request.get_json()
    function = data.get("function")
    # args = data.get("args")  # Expecting list/tuple

    # Mapping function names to actual calls
    # This is a security boundary; only allow specific functions
    if function == "mark_as_read_job":
        # args: (chat_id, topic_id, max_id)
        # We need to parse args back from string if they were stored as string representation
        # Ideally QueueMonitor stores JSON.
        # For Phase 4 MVP, we'll implement a "Flush/Clear" only,
        # as strict retry logic requires robust serialization in QueueMonitor.
        pass

    # For now, we will just support Clearing the log visually
    # Real retry logic requires storing args as structured JSON in QueueMonitor
    return jsonify({"status": "not_implemented_yet"})


@queue_bp.route("/api/queue/clear", methods=["POST"])
async def clear_failed_jobs():
    monitor = get_queue_monitor()
    # Manually remove all from ZSET
    await monitor.redis.delete(monitor.dead_letter_key)

    if request.headers.get("HX-Request"):
        return await render_template("partials/queue_status.html.j2", failed_jobs=[])

    return jsonify({"status": "cleared"})
