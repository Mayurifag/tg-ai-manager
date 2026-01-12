from quart import Blueprint, jsonify, render_template, request

from src.config import get_settings
from src.container import _get_tg_adapter, get_rule_service, get_user_repo
from src.rules.models import RuleType
from src.rules.sqlite_repo import SqliteRuleRepository
from src.users.models import User

settings_bp = Blueprint("settings", __name__)


@settings_bp.route("/settings", methods=["GET"])
async def settings_view():
    settings = get_settings()
    user_repo = get_user_repo()
    rule_repo = SqliteRuleRepository(db_path=settings.DB_PATH)

    # 1. Fetch Global User Settings
    user = await user_repo.get_user(1)
    if not user:
        user = User()

    # 2. Fetch Active Rules for Summary
    all_rules = await rule_repo.get_all()
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
        "settings/settings.html.j2", settings=user, grouped_rules=grouped_rules
    )


@settings_bp.route("/api/settings", methods=["PATCH", "POST"])
async def save_settings():
    repo = get_user_repo()
    data = await request.get_json()

    current_user = await repo.get_user(1)
    if not current_user:
        return jsonify({"error": "User not found"}), 404

    # Helper to get value from data or fallback to current
    def get_val(key, default):
        return data[key] if key in data else default

    updated_user = User(
        id=current_user.id,
        api_id=current_user.api_id,
        api_hash=current_user.api_hash,
        username=current_user.username,
        session_string=current_user.session_string,
        # Preserves existing value if key not sent in JSON
        autoread_service_messages=bool(
            get_val("autoread_service_messages", current_user.autoread_service_messages)
        ),
        autoread_polls=bool(get_val("autoread_polls", current_user.autoread_polls)),
        autoread_self=bool(get_val("autoread_self", current_user.autoread_self)),
        autoread_bots=get_val("autoread_bots", current_user.autoread_bots),
        autoread_regex=get_val("autoread_regex", current_user.autoread_regex),
        # Debug Mode
        debug_mode=bool(get_val("debug_mode", current_user.debug_mode)),
    )

    await repo.save_user(updated_user)
    return jsonify({"status": "ok"})


@settings_bp.route("/api/settings/reset", methods=["POST"])
async def reset_account():
    adapter = _get_tg_adapter()
    if adapter:
        await adapter.disconnect()

    repo = get_user_repo()
    await repo.delete_user(1)

    return jsonify({"status": "ok"})


# --- Rule API Endpoints ---


@settings_bp.route("/api/rules/autoread/toggle", methods=["POST"])
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


@settings_bp.route("/api/rules/autoread/apply_all", methods=["POST"])
async def api_apply_autoread_all_topics():
    rule_service = get_rule_service()
    data = await request.get_json()
    forum_id = data.get("forum_id")
    enabled = data.get("enabled", True)

    if not forum_id:
        return jsonify({"error": "forum_id required"}), 400

    await rule_service.apply_autoread_to_all_topics(forum_id, enabled)
    return jsonify({"status": "ok"})


@settings_bp.route("/api/rules/autoreact/config", methods=["POST"])
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


@settings_bp.route("/api/rules/autoreact/get", methods=["GET"])
async def api_get_autoreact():
    rule_service = get_rule_service()
    chat_id = request.args.get("chat_id", type=int)
    topic_id = request.args.get("topic_id", type=int)

    if not chat_id:
        return jsonify({"error": "chat_id required"}), 400

    rule = await rule_service.get_rule(chat_id, topic_id, RuleType.AUTOREACT)

    if rule:
        return jsonify({"enabled": True, "config": rule.config})
    return jsonify({"enabled": False, "config": {}})


@settings_bp.route("/api/debug/process", methods=["POST"])
async def api_debug_process():
    rule_service = get_rule_service()
    data = await request.get_json()
    chat_id = data.get("chat_id")
    msg_id = data.get("msg_id")

    if not chat_id or not msg_id:
        return jsonify({"error": "chat_id and msg_id required"}), 400

    result = await rule_service.simulate_process_message(chat_id, msg_id)
    return jsonify(result)
