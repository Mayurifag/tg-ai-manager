import hmac
import re

from quart import Blueprint, current_app, jsonify, render_template, request

from src.config import get_settings
from src.container import (
    _get_tg_adapter,
    get_rule_service,
    get_user_repo,
    reload_tg_adapter,
)
from src.rules.models import DEFAULT_AUTOREACT_EMOJI, RuleType
from src.rules.ports import InheritedRuleError
from src.rules.sqlite_repo import SqliteRuleRepository
from src.users.models import User
from src.requests import optional_int, require_int
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


def rule_result(rule, topic_id):
    return jsonify(
        {
            "status": "ok",
            "enabled": bool(rule),
            "inherited": bool(rule and topic_id is not None and rule.topic_id is None),
        }
    )


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

    updated = await repo.update_fields(
        current_user.id, patch.values, expected_session=current_user.session_string
    )
    if not updated:
        return jsonify({"error": "Account changed; retry the request"}), 409
    if patch.values.keys() & {"ai_provider", "ai_model", "ai_api_key", "ai_prompt"}:
        await get_rule_service().invalidate_ai()
    return jsonify({"status": "ok"})


@settings_bp.route("/api/settings/reset", methods=["POST"])
async def reset_account():
    async with current_app.login_lock:
        await reload_tg_adapter(session_string=None)
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

    try:
        rule = await rule_service.toggle_autoread(
            body.chat_id, body.topic_id, body.enabled
        )
    except InheritedRuleError as e:
        return jsonify({"error": str(e)}), 409
    return rule_result(rule, body.topic_id)


@settings_bp.route("/api/rules/ai_autoread/toggle", methods=["POST"])
async def api_toggle_ai_autoread():
    rule_service = get_rule_service()
    try:
        body = ToggleRuleRequest.from_json(await request.get_json())
    except BadRequest as e:
        return bad_request(e)

    try:
        rule = await rule_service.toggle_ai_autoread(
            body.chat_id, body.topic_id, body.enabled
        )
    except InheritedRuleError as e:
        return jsonify({"error": str(e)}), 409
    return rule_result(rule, body.topic_id)


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

    try:
        rule = await rule_service.set_autoreact(
            body.chat_id, body.topic_id, body.enabled, body.config
        )
    except InheritedRuleError as e:
        return jsonify({"error": str(e)}), 409
    return rule_result(rule, body.topic_id)


@settings_bp.route("/api/rules/autoreact/get", methods=["GET"])
async def api_get_autoreact():
    rule_service = get_rule_service()
    try:
        params = request.args.to_dict()
        chat_id = require_int(params, "chat_id")
        topic_id = optional_int(params, "topic_id")
    except BadRequest as e:
        return bad_request(e)

    rule = await rule_service.get_rule(chat_id, topic_id, RuleType.AUTOREACT)

    if rule:
        return jsonify(
            {
                "enabled": True,
                "config": rule.config,
                "inherited": bool(topic_id and rule.topic_id is None),
            }
        )
    return jsonify({"enabled": False, "config": {}, "inherited": False})


@settings_bp.route("/api/debug/process", methods=["POST"])
async def api_debug_process():
    rule_service = get_rule_service()
    try:
        body = DebugProcessRequest.from_json(await request.get_json())
    except BadRequest as e:
        return bad_request(e)

    result = await rule_service.simulate_process_message(
        body.chat_id, body.msg_id, topic_id=body.topic_id, evaluate_ai=body.evaluate_ai
    )
    return jsonify(result), 400 if "error" in result else 200


@settings_bp.route("/api/rules/<int:rule_id>", methods=["DELETE"])
async def api_delete_rule(rule_id: int):
    settings = get_settings()
    rule_repo = SqliteRuleRepository(db_path=settings.DB_PATH)
    await rule_repo.delete(rule_id)
    return jsonify({"status": "ok"})


@settings_bp.route("/api/rules/export", methods=["GET"])
async def api_export_rules():
    settings = get_settings()
    expected = settings.RULES_EXPORT_TOKEN
    authorization = request.headers.get("Authorization", "")
    supplied = (
        authorization.removeprefix("Bearer ")
        if authorization.startswith("Bearer ")
        else ""
    )
    if (
        not isinstance(expected, str)
        or not re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", expected, re.ASCII)
        or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("ascii"))
    ):
        response = jsonify({"error": "Export authorization required"})
        response.headers["Cache-Control"] = "no-store"
        return response, 403
    # Account replacement and reset use this same lock. Do not label rules
    # fetched from one account with a subsequently activated account's ID.
    async with current_app.login_lock:
        rule_repo = SqliteRuleRepository(db_path=settings.DB_PATH)
        all_rules = await rule_repo.get_all()
        user = await get_user_repo().get_user(1)
        adapter = _get_tg_adapter()
        account_id = (
            getattr(adapter, "_self_id", None)
            if adapter and adapter.is_connected()
            else None
        )
    if not user:
        user = User()

    rules_list = []
    for rule in all_rules:
        config = rule.config
        if rule.rule_type == RuleType.AUTOREACT and isinstance(config, dict):
            # Older saved rules may omit fields that were implicit at runtime.
            # Make those defaults explicit without weakening import validation.
            config = dict(config)
            config.setdefault("emoji", DEFAULT_AUTOREACT_EMOJI)
            if config.get("target_users") is None:
                config["target_users"] = []
        rules_list.append(
            {
                "rule_type": rule.rule_type.value,
                "chat_id": rule.chat_id,
                "topic_id": rule.topic_id,
                "config": config,
            }
        )

    user_settings = {
        "autoread_service_messages": user.autoread_service_messages,
        "autoread_polls": user.autoread_polls,
        "autoread_self": user.autoread_self,
        "autoread_bots": user.autoread_bots,
        "autoread_regex": user.autoread_regex,
        "debug_mode": user.debug_mode,
        "ai_provider": user.ai_provider,
        "ai_model": user.ai_model,
        "ai_prompt": user.ai_prompt,
    }

    if type(account_id) is not int or account_id <= 0:
        account_id = None
    response = jsonify(
        {"account_id": account_id, "rules": rules_list, "user_settings": user_settings}
    )
    response.headers["Cache-Control"] = "no-store"
    return response


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
