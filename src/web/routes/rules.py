from quart import Blueprint, jsonify, render_template, request

from src.config import get_settings
from src.container import get_rule_service, get_user_repo
from src.rules.sqlite_repo import SqliteRuleRepository

rules_bp = Blueprint("rules", __name__)


@rules_bp.route("/rules", methods=["GET"])
async def rules_summary_view():
    settings = get_settings()
    rule_repo = SqliteRuleRepository(db_path=settings.DB_PATH)
    user_repo = get_user_repo()

    # Fetch Global Settings (User 1)
    user = await user_repo.get_user(1)

    # Fetch all specific rules
    all_rules = await rule_repo.get_all()

    # Group rules by Chat ID
    # Structure: { chat_id: { 'chat_rules': [Rule], 'topics': { topic_id: [Rule] } } }
    grouped_rules = {}

    for rule in all_rules:
        cid = rule.chat_id
        if cid not in grouped_rules:
            grouped_rules[cid] = {"chat_rules": [], "topics": {}}

        if rule.topic_id is None:
            grouped_rules[cid]["chat_rules"].append(rule)
        else:
            if rule.topic_id not in grouped_rules[cid]["topics"]:
                grouped_rules[cid]["topics"][rule.topic_id] = []
            grouped_rules[cid]["topics"][rule.topic_id].append(rule)

    return await render_template(
        "rules/summary.html.j2", grouped_rules=grouped_rules, global_settings=user
    )


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
