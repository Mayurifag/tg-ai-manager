import traceback
from quart import Blueprint, render_template, request, jsonify, redirect, url_for
from telethon import utils
from telethon.tl.types import auth
from src.container import get_user_repo, reload_tg_adapter, _get_tg_adapter
from src.users.models import User
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)
auth_bp = Blueprint("auth", __name__)

_auth_state = {}

@auth_bp.route("/login", methods=["GET"])
async def login_page():
    repo = get_user_repo()
    user = await repo.get_user(1)
    if user and user.is_authenticated():
        adapter = _get_tg_adapter()
        if adapter.is_connected():
            return redirect("/")

    return await render_template("auth/login.html.j2")

# --- Phone / SMS Flows ---

@auth_bp.route("/api/auth/send_code", methods=["POST"])
async def send_code():
    try:
        data = await request.get_json()
        api_id = int(data.get("api_id"))
        api_hash = data.get("api_hash")
        phone = data.get("phone")

        logger.info("auth_send_code_init", phone=phone, api_id=api_id)

        # Reuse existing adapter if credentials match to preserve session
        current_adapter = _get_tg_adapter()
        should_reload = True

        if current_adapter and current_adapter.api_id == api_id and current_adapter.api_hash == api_hash:
            logger.info("auth_reusing_adapter")
            should_reload = False
            adapter = current_adapter

        if should_reload:
            reload_tg_adapter(api_id, api_hash, session_string=None)
            adapter = _get_tg_adapter()

        _auth_state["api_id"] = api_id
        _auth_state["api_hash"] = api_hash
        _auth_state["phone"] = phone

        sent = await adapter.send_code(phone)

        _auth_state["phone_code_hash"] = sent.phone_code_hash

        code_type_str = _get_code_type_label(sent.type)

        logger.info("auth_send_code_sent", type=code_type_str)
        return jsonify({"status": "ok", "code_type": code_type_str})

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error("auth_send_code_failed", error=str(e), traceback=traceback.format_exc())
        return jsonify({"error": str(e)}), 400

@auth_bp.route("/api/auth/resend_code", methods=["POST"])
async def resend_code():
    try:
        phone = _auth_state.get("phone")
        phone_code_hash = _auth_state.get("phone_code_hash")

        if not phone or not phone_code_hash:
            return jsonify({"error": "Session expired, please restart login."}), 400

        adapter = _get_tg_adapter()
        sent = await adapter.resend_code(phone, phone_code_hash)

        # Update hash (it might change)
        _auth_state["phone_code_hash"] = sent.phone_code_hash

        code_type_str = _get_code_type_label(sent.type)
        logger.info("auth_resend_code_sent", type=code_type_str)

        return jsonify({"status": "ok", "code_type": code_type_str})

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error("auth_resend_code_failed", error=str(e), traceback=traceback.format_exc())
        return jsonify({"error": str(e)}), 400

@auth_bp.route("/api/auth/login", methods=["POST"])
async def login_verify():
    try:
        data = await request.get_json()
        code = data.get("code")

        phone = _auth_state.get("phone")
        phone_code_hash = _auth_state.get("phone_code_hash")

        if not phone or not phone_code_hash:
            return jsonify({"error": "Session expired, refresh page"}), 400

        adapter = _get_tg_adapter()

        result = await adapter.sign_in(phone, code, phone_code_hash)

        if result == "needs_password":
            return jsonify({"status": "needs_password"})

        if result == "logged_in":
            await _finalize_login(adapter)
            return jsonify({"status": "ok"})

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error("auth_login_verify_failed", error=str(e), traceback=traceback.format_exc())
        return jsonify({"error": str(e)}), 400

# --- QR Flow ---

@auth_bp.route("/api/auth/qr/start", methods=["POST"])
async def qr_start():
    try:
        data = await request.get_json()
        api_id = int(data.get("api_id"))
        api_hash = data.get("api_hash")

        _auth_state["api_id"] = api_id
        _auth_state["api_hash"] = api_hash

        # Initialize Adapter
        reload_tg_adapter(api_id, api_hash, session_string=None)
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

# --- 2FA / Password ---

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

# --- Helper ---

async def _finalize_login(adapter):
    session_string = adapter.get_session_string()

    me = await adapter.client.get_me()
    username = getattr(me, 'username', None)
    if not username:
        username = utils.get_display_name(me)
    else:
        username = f"@{username}"

    repo = get_user_repo()
    existing_user = await repo.get_user(1)

    updated_user = User(
        id=1,
        api_id=_auth_state["api_id"],
        api_hash=_auth_state["api_hash"],
        username=username,
        session_string=session_string,
        # Preserve settings
        autoread_service_messages=existing_user.autoread_service_messages if existing_user else False,
        autoread_polls=existing_user.autoread_polls if existing_user else False,
        autoread_self=existing_user.autoread_self if existing_user else False,
        autoread_bots=existing_user.autoread_bots if existing_user else "@lolsBotCatcherBot",
        autoread_regex=existing_user.autoread_regex if existing_user else ""
    )

    await repo.save_user(updated_user)
    _auth_state.clear()
    logger.info("auth_login_completed", username=username)

def _get_code_type_label(sent_type) -> str:
    if isinstance(sent_type, auth.SentCodeTypeApp):
        return "Telegram App"
    elif isinstance(sent_type, auth.SentCodeTypeSms):
        return "SMS"
    elif isinstance(sent_type, auth.SentCodeTypeCall):
        return "Phone Call"
    elif isinstance(sent_type, auth.SentCodeTypeFlashCall):
        return "Flash Call"
    return "Unknown"
