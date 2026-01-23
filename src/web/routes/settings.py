from quart import Blueprint, jsonify, render_template, request

from src.config import get_settings
from src.container import get_user_repo
from src.infrastructure.security import CryptoManager
from src.rules.sqlite_repo import SqliteRuleRepository
from src.settings.models import GlobalSettings
from src.settings.sqlite_repo import SqliteSettingsRepository

settings_bp = Blueprint("settings", __name__)


@settings_bp.route("/settings", methods=["GET"])
async def settings_view():
    settings_cfg = get_settings()
    settings_repo = SqliteSettingsRepository(db_path=settings_cfg.DB_PATH)
    rule_repo = SqliteRuleRepository(db_path=settings_cfg.DB_PATH)

    # 1. Fetch User (Legacy) and Global Settings (New)
    # We are transitioning settings from User model to GlobalSettings table for AI stuff
    # For now, we mix them or render both.
    # To keep it clean, we fetch GlobalSettings and pass it to template.

    global_settings = await settings_repo.get_settings()

    # Sync legacy fields from user repo if needed, but for now we assume migration
    # moved them or we use GlobalSettings as source of truth for AI.
    # The current template expects 'settings' object.

    # 2. Fetch Rules
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
        "settings/settings_wrapper.html.j2",
        settings=global_settings,
        grouped_rules=grouped_rules,
    )


@settings_bp.route("/api/settings", methods=["PATCH", "POST"])
async def save_settings():
    settings_cfg = get_settings()
    repo = SqliteSettingsRepository(db_path=settings_cfg.DB_PATH)
    crypto = CryptoManager()

    current = await repo.get_settings()
    data = await request.get_json()

    # Handle encryption for API Key
    api_key = current.ai_api_key
    if "ai_api_key" in data and data["ai_api_key"]:
        api_key = crypto.encrypt(data["ai_api_key"])

    updated = GlobalSettings(
        id=1,
        autoread_service_messages=data.get(
            "autoread_service_messages", current.autoread_service_messages
        ),
        autoread_polls=data.get("autoread_polls", current.autoread_polls),
        autoread_bots=data.get("autoread_bots", current.autoread_bots),
        autoread_regex=data.get("autoread_regex", current.autoread_regex),
        autoread_self=data.get("autoread_self", current.autoread_self),
        ai_enabled=data.get("ai_enabled", current.ai_enabled),
        ai_provider=data.get("ai_provider", current.ai_provider),
        ai_model=data.get("ai_model", current.ai_model),
        ai_api_key=api_key,
        ai_base_url=data.get("ai_base_url", current.ai_base_url),
        skip_ads_enabled=data.get("skip_ads_enabled", current.skip_ads_enabled),
    )

    await repo.save_settings(updated)

    # Also update User table for legacy compatibility (debug_mode is there)
    user_repo = get_user_repo()
    user = await user_repo.get_user(1)
    if user and "debug_mode" in data:
        user.debug_mode = data["debug_mode"]
        await user_repo.save_user(user)

    return jsonify({"status": "ok"})
