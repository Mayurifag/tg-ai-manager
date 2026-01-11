from quart import Blueprint, jsonify, request

from src.container import get_rule_service

rules_bp = Blueprint("rules", __name__)


@rules_bp.route("/api/rules/autoread/toggle", methods=["POST"])
async def api_toggle_autoread():
    rule_service = get_rule_service()
    data = await request.get_json()
    chat_id = data.get("chat_id")
    topic_id = data.get("topic_id")
    enabled = data.get("enabled", True)

    if not chat_id:
        return jsonify({"error": "chat_id required"}), 400

    await rule_service.toggle_autoread(chat_id, topic_id, enabled)
    return jsonify({"status": "ok"})


@rules_bp.route("/api/rules/autoread/apply_all", methods=["POST"])
async def api_apply_autoread_all_topics():
    rule_service = get_rule_service()
    data = await request.get_json()
    forum_id = data.get("forum_id")
    enabled = data.get("enabled", True)

    if not forum_id:
        return jsonify({"error": "forum_id required"}), 400

    await rule_service.apply_autoread_to_all_topics(forum_id, enabled)
    return jsonify({"status": "ok"})


@rules_bp.route("/api/rules/autoreact/config", methods=["POST"])
async def api_set_autoreact():
    rule_service = get_rule_service()
    data = await request.get_json()
    chat_id = data.get("chat_id")
    topic_id = data.get("topic_id")
    enabled = data.get("enabled", False)
    config = data.get("config", {})  # {emoji: "💩", target_users: []}

    if not chat_id:
        return jsonify({"error": "chat_id required"}), 400

    await rule_service.set_autoreact(chat_id, topic_id, enabled, config)
    return jsonify({"status": "ok"})


@rules_bp.route("/api/rules/autoreact/get", methods=["GET"])
async def api_get_autoreact():
    rule_service = get_rule_service()
    chat_id = request.args.get("chat_id", type=int)
    topic_id = request.args.get("topic_id", type=int)

    if not chat_id:
        return jsonify({"error": "chat_id required"}), 400

    from src.rules.models import RuleType

    rule = await rule_service.get_rule(chat_id, topic_id, RuleType.AUTOREACT)

    if rule:
        return jsonify({"enabled": True, "config": rule.config})
    return jsonify({"enabled": False, "config": {}})


@rules_bp.route("/api/debug/process", methods=["POST"])
async def api_debug_process():
    rule_service = get_rule_service()
    data = await request.get_json()
    chat_id = data.get("chat_id")
    msg_id = data.get("msg_id")

    if not chat_id or not msg_id:
        return jsonify({"error": "chat_id and msg_id required"}), 400

    result = await rule_service.simulate_process_message(chat_id, msg_id)
    return jsonify(result)
