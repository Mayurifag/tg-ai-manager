from quart import Blueprint, jsonify, render_template, request

from src.config import get_settings
from src.container import _get_tg_adapter, get_rule_service, get_user_repo
from src.rules.models import RuleType
from src.rules.sqlite_repo import SqliteRuleRepository
from src.users.models import User
from src.web.requests import (
    ApplyAllTopicsRequest,
    AutoreactConfigRequest,
    BadRequest,
    DebugProcessRequest,
    SettingsPatch,
    ToggleRuleRequest,
)

settings_bp = Blueprint("settings", __name__)


def bad_request(error: BadRequest):
    return jsonify({"error": str(error)}), 400


@settings_bp.route("/settings", methods=["GET"])
async def settings_view():
    user_repo = get_user_repo()
    user = await user_repo.get_user(1)
    if not user:
        user = User()
    return await render_template("settings/settings.html.j2", settings=user)


@settings_bp.route("/api/settings", methods=["PATCH", "POST"])
async def save_settings():
    repo = get_user_repo()
    try:
        patch = SettingsPatch.from_json(await request.get_json())
    except BadRequest as e:
        return bad_request(e)

    current_user = await repo.get_user(1)
    if not current_user:
        return jsonify({"error": "User not found"}), 404

    updated_user = User(
        id=current_user.id,
        api_id=current_user.api_id,
        api_hash=current_user.api_hash,
        username=current_user.username,
        session_string=current_user.session_string,
        autoread_service_messages=bool(
            patch.get(
                "autoread_service_messages", current_user.autoread_service_messages
            )
        ),
        autoread_polls=bool(patch.get("autoread_polls", current_user.autoread_polls)),
        autoread_self=bool(patch.get("autoread_self", current_user.autoread_self)),
        autoread_bots=patch.get("autoread_bots", current_user.autoread_bots),
        autoread_regex=patch.get("autoread_regex", current_user.autoread_regex),
        debug_mode=bool(patch.get("debug_mode", current_user.debug_mode)),
        ai_provider=patch.get("ai_provider", current_user.ai_provider),
        ai_model=patch.get("ai_model", current_user.ai_model),
        ai_api_key=patch.get("ai_api_key", current_user.ai_api_key),
        ai_prompt=patch.get("ai_prompt", current_user.ai_prompt),
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
    try:
        body = ToggleRuleRequest.from_json(await request.get_json())
    except BadRequest as e:
        return bad_request(e)

    await rule_service.toggle_autoread(body.chat_id, body.topic_id, body.enabled)
    return jsonify({"status": "ok"})


@settings_bp.route("/api/rules/ai_autoread/toggle", methods=["POST"])
async def api_toggle_ai_autoread():
    rule_service = get_rule_service()
    try:
        body = ToggleRuleRequest.from_json(await request.get_json())
    except BadRequest as e:
        return bad_request(e)

    await rule_service.toggle_ai_autoread(body.chat_id, body.topic_id, body.enabled)
    return jsonify({"status": "ok"})


@settings_bp.route("/api/rules/autoread/apply_all", methods=["POST"])
async def api_apply_autoread_all_topics():
    rule_service = get_rule_service()
    try:
        body = ApplyAllTopicsRequest.from_json(await request.get_json())
    except BadRequest as e:
        return bad_request(e)

    await rule_service.apply_autoread_to_all_topics(body.forum_id, body.enabled)
    return jsonify({"status": "ok"})


@settings_bp.route("/api/rules/autoreact/config", methods=["POST"])
async def api_set_autoreact():
    rule_service = get_rule_service()
    try:
        body = AutoreactConfigRequest.from_json(await request.get_json())
    except BadRequest as e:
        return bad_request(e)

    await rule_service.set_autoreact(
        body.chat_id, body.topic_id, body.enabled, body.config
    )
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
    try:
        body = DebugProcessRequest.from_json(await request.get_json())
    except BadRequest as e:
        return bad_request(e)

    result = await rule_service.simulate_process_message(body.chat_id, body.msg_id)
    return jsonify(result)


@settings_bp.route("/api/rules/<int:rule_id>", methods=["DELETE"])
async def api_delete_rule(rule_id: int):
    settings = get_settings()
    rule_repo = SqliteRuleRepository(db_path=settings.DB_PATH)
    await rule_repo.delete(rule_id)
    return jsonify({"status": "ok"})


@settings_bp.route("/api/rules/export", methods=["GET"])
async def api_export_rules():
    settings = get_settings()
    rule_repo = SqliteRuleRepository(db_path=settings.DB_PATH)
    all_rules = await rule_repo.get_all()

    user_repo = get_user_repo()
    user = await user_repo.get_user(1)
    if not user:
        user = User()

    rules_list = [
        {
            "rule_type": rule.rule_type.value,
            "chat_id": rule.chat_id,
            "topic_id": rule.topic_id,
            "config": rule.config,
        }
        for rule in all_rules
    ]

    user_settings = {
        "autoread_service_messages": user.autoread_service_messages,
        "autoread_polls": user.autoread_polls,
        "autoread_self": user.autoread_self,
        "autoread_bots": user.autoread_bots,
        "autoread_regex": user.autoread_regex,
        "debug_mode": user.debug_mode,
        "ai_provider": user.ai_provider,
        "ai_model": user.ai_model,
        "ai_api_key": user.ai_api_key,
        "ai_prompt": user.ai_prompt,
    }

    return jsonify({"rules": rules_list, "user_settings": user_settings})


@settings_bp.route("/api/rules", methods=["GET"])
async def api_get_all_rules():
    settings = get_settings()
    rule_repo = SqliteRuleRepository(db_path=settings.DB_PATH)
    all_rules = await rule_repo.get_all()
    grouped: dict = {}
    for rule in all_rules:
        cid = rule.chat_id
        if cid not in grouped:
            grouped[cid] = {"chat_rules": [], "topics": {}}
        if rule.topic_id is None:
            grouped[cid]["chat_rules"].append(
                {
                    "id": rule.id,
                    "rule_type": rule.rule_type.value,
                    "config": rule.config,
                    "topic_id": None,
                }
            )
        else:
            tid = rule.topic_id
            if tid not in grouped[cid]["topics"]:
                grouped[cid]["topics"][tid] = []
            grouped[cid]["topics"][tid].append(
                {
                    "id": rule.id,
                    "rule_type": rule.rule_type.value,
                    "config": rule.config,
                    "topic_id": tid,
                }
            )
    return jsonify(grouped)
