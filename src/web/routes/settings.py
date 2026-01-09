from quart import Blueprint, render_template, request, jsonify
from src.users.models import User
from src.container import get_user_repo, _get_tg_adapter

settings_bp = Blueprint("settings", __name__)


@settings_bp.route("/settings", methods=["GET"])
async def settings_view():
    repo = get_user_repo()
    user = await repo.get_user(1)
    if not user:
        user = User()  # Default
    return await render_template("settings/settings.html.j2", settings=user)


@settings_bp.route("/api/settings", methods=["POST"])
async def save_settings():
    repo = get_user_repo()
    data = await request.get_json()

    current_user = await repo.get_user(1)
    if not current_user:
        # Should not happen if logged in
        return jsonify({"error": "User not found"}), 404

    updated_user = User(
        id=current_user.id,
        # Preserve Credentials & Identity
        api_id=current_user.api_id,
        api_hash=current_user.api_hash,
        username=current_user.username,
        session_string=current_user.session_string,
        # Update Settings
        autoread_service_messages=bool(data.get("autoread_service_messages", False)),
        autoread_polls=bool(data.get("autoread_polls", False)),
        autoread_self=bool(data.get("autoread_self", False)),
        autoread_bots=data.get("autoread_bots", ""),
        autoread_regex=data.get("autoread_regex", ""),
    )

    await repo.save_user(updated_user)
    return jsonify({"status": "ok"})


@settings_bp.route("/api/settings/reset", methods=["POST"])
async def reset_account():
    # 1. Disconnect Telegram Adapter
    adapter = _get_tg_adapter()
    if adapter:
        await adapter.disconnect()

    # 2. Delete User (and cascades to rules)
    repo = get_user_repo()
    await repo.delete_user(1)

    return jsonify({"status": "ok"})
