from quart import Blueprint, render_template, request, jsonify
from src.settings.models import GlobalSettings
from src.container import _get_settings_repo

settings_bp = Blueprint("settings", __name__)


@settings_bp.route("/settings", methods=["GET"])
async def settings_view():
    repo = _get_settings_repo()
    settings = await repo.get_settings()
    return await render_template("settings/settings.html.j2", settings=settings)


@settings_bp.route("/api/settings", methods=["POST"])
async def save_settings():
    repo = _get_settings_repo()
    data = await request.get_json()

    # Removed autoread_only_new from mapping
    settings = GlobalSettings(
        id=1,
        autoread_service_messages=bool(data.get("autoread_service_messages", False)),
        autoread_polls=bool(data.get("autoread_polls", False)),
        autoread_bots=data.get("autoread_bots", ""),
        autoread_regex=data.get("autoread_regex", ""),
        autoread_self=bool(data.get("autoread_self", False)),
    )

    await repo.save_settings(settings)
    return jsonify({"status": "ok"})
