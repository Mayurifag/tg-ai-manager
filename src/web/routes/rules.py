from quart import Blueprint, request, jsonify
from src.container import get_rule_service

rules_bp = Blueprint('rules', __name__)

@rules_bp.route("/api/rules/autoread/toggle", methods=["POST"])
async def api_toggle_autoread():
    rule_service = get_rule_service()
    data = await request.get_json()
    chat_id = data.get("chat_id")
    topic_id = data.get("topic_id")  # can be None
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
