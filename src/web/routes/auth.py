import traceback
from quart import Blueprint, render_template, jsonify, redirect, request
from telethon import utils
from src.container import get_user_repo, reload_tg_adapter, _get_tg_adapter
from src.users.models import User
from src.infrastructure.logging import get_logger
from src.config import get_settings

logger = get_logger(__name__)
auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET"])
async def login_page():
    repo = get_user_repo()
    user = await repo.get_user(1)
    if user and user.is_authenticated():
        adapter = _get_tg_adapter()
        if adapter.is_connected():
            return redirect("/")

    return await render_template("auth/login.html.j2")


@auth_bp.route("/api/auth/qr/start", methods=["POST"])
async def qr_start():
    try:
        settings = get_settings()

        reload_tg_adapter(
            api_id=settings.TG_API_ID,
            api_hash=settings.TG_API_HASH,
            session_string=None,
        )
        adapter = _get_tg_adapter()

        url = await adapter.start_qr_login()
        if url == "authorized":
            return jsonify({"status": "authorized"})

        return jsonify({"status": "ok", "url": url})
    except Exception as e:
        logger.error("qr_start_failed", error=str(e), traceback=traceback.format_exc())
        return jsonify({"error": str(e)}), 400


@auth_bp.route("/api/auth/qr/status", methods=["GET"])
async def qr_status():
    adapter = _get_tg_adapter()
    if not adapter:
        return jsonify({"status": "none"})

    status = adapter.get_qr_status()
    if status == "authorized":
        await _finalize_login(adapter)
        return jsonify({"status": "authorized"})

    return jsonify({"status": status})


@auth_bp.route("/api/auth/hint", methods=["GET"])
async def get_password_hint():
    adapter = _get_tg_adapter()
    hint = await adapter.get_password_hint()
    return jsonify({"hint": hint})


@auth_bp.route("/api/auth/2fa", methods=["POST"])
async def login_2fa():
    try:
        data = await request.get_json()
        password = data.get("password")
        adapter = _get_tg_adapter()

        await adapter.sign_in_with_password(password)
        await _finalize_login(adapter)
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error("auth_2fa_failed", error=str(e), traceback=traceback.format_exc())
        return jsonify({"error": str(e)}), 400


async def _finalize_login(adapter):
    session_string = adapter.get_session_string()
    settings = get_settings()

    me = await adapter.client.get_me()
    username = getattr(me, "username", None)
    if not username:
        username = utils.get_display_name(me)
    else:
        username = f"@{username}"

    repo = get_user_repo()
    existing_user = await repo.get_user(1)

    updated_user = User(
        id=1,
        api_id=settings.TG_API_ID,
        api_hash=settings.TG_API_HASH,
        username=username,
        session_string=session_string,
        autoread_service_messages=existing_user.autoread_service_messages
        if existing_user
        else False,
        autoread_polls=existing_user.autoread_polls if existing_user else False,
        autoread_self=existing_user.autoread_self if existing_user else False,
        autoread_bots=existing_user.autoread_bots
        if existing_user
        else "@lolsBotCatcherBot",
        autoread_regex=existing_user.autoread_regex if existing_user else "",
    )

    await repo.save_user(updated_user)
    logger.info("auth_login_completed", username=username)
