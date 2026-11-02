import secrets

from quart import Blueprint, current_app, jsonify, redirect, render_template, request
from telethon import utils
from telethon.errors import PasswordHashInvalidError

from src.config import get_settings
from src.container import (
    _get_tg_adapter,
    get_rule_service,
    get_user_repo,
    reload_tg_adapter,
)
from src.infrastructure.logging import get_logger
from src.rules.sync import sync_rules_from_remote
from src.users.models import User
from src.web.requests import BadRequest, PasswordRequest

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
        async with current_app.login_lock:
            user = await get_user_repo().get_user(1)
            current = _get_tg_adapter()
            expected_account_id = None
            if user and user.session_string:
                if current._login_attempt_id:
                    expected_account_id = current._expected_login_account_id
                elif current._source_session_string == user.session_string:
                    expected_account_id = current._self_id
                if type(expected_account_id) is not int or expected_account_id <= 0:
                    return jsonify(
                        {
                            "error": "Cannot verify the saved account while disconnected. Reconnect it before signing in again."
                        }
                    ), 409
            await reload_tg_adapter(session_string=None)
            adapter = _get_tg_adapter()
            adapter._expected_login_account_id = expected_account_id
            adapter._login_attempt_id = secrets.token_urlsafe(24)
            url = await adapter.start_qr_login()
            if url == "authorized":
                await _finalize_login_locked(adapter)
                adapter._login_finalized = True
                return jsonify({"status": "authorized"})

            return jsonify(
                {"status": "ok", "url": url, "attempt_id": adapter._login_attempt_id}
            )
    except Exception as e:
        logger.error("qr_start_failed", category=type(e).__name__)
        return jsonify({"error": "Could not start login"}), 400


@auth_bp.route("/api/auth/qr/status", methods=["GET"])
async def qr_status():
    async with current_app.login_lock:
        adapter = _get_tg_adapter()
        if not _matches_attempt(adapter, request.args.get("attempt_id")):
            return jsonify({"error": "Login attempt was superseded"}), 409
        status = adapter.get_qr_status()
        if status == "authorized":
            try:
                if not adapter._login_finalized:
                    await _finalize_login_locked(adapter)
                    adapter._login_finalized = True
            except AccountMismatchError:
                return jsonify(
                    {
                        "error": "This is a different Telegram account. Forget the saved account before switching."
                    }
                ), 409
            except Exception as exc:
                logger.error("auth_finalize_failed", category=type(exc).__name__)
                return jsonify({"error": "Could not complete login"}), 400
        return jsonify({"status": status})


@auth_bp.route("/api/auth/hint", methods=["GET"])
async def get_password_hint():
    async with current_app.login_lock:
        adapter = _get_tg_adapter()
        if not _matches_attempt(adapter, request.args.get("attempt_id")):
            return jsonify({"error": "Login attempt was superseded"}), 409
        hint = await adapter.get_password_hint()
        return jsonify({"hint": hint})


@auth_bp.route("/api/auth/2fa", methods=["POST"])
async def login_2fa():
    try:
        data = await request.get_json()
        body = PasswordRequest.from_json(data)
        async with current_app.login_lock:
            adapter = _get_tg_adapter()
            if not _matches_attempt(adapter, data.get("attempt_id")):
                return jsonify({"error": "Login attempt was superseded"}), 409
            if not adapter._login_finalized:
                if adapter.get_qr_status() != "authorized":
                    await adapter.sign_in_with_password(body.password)
                await _finalize_login_locked(adapter)
                adapter._login_finalized = True
        return jsonify({"status": "ok"})
    except BadRequest as e:
        return jsonify({"error": str(e)}), 400
    except PasswordHashInvalidError:
        return jsonify({"error": "Incorrect password"}), 400
    except AccountMismatchError:
        return jsonify(
            {
                "error": "This is a different Telegram account. Forget the saved account before switching."
            }
        ), 409
    except Exception as e:
        logger.error("auth_2fa_failed", category=type(e).__name__)
        return jsonify({"error": "Could not complete login"}), 400


class AccountMismatchError(RuntimeError):
    pass


def _matches_attempt(adapter, attempt_id) -> bool:
    return (
        adapter is not None
        and isinstance(attempt_id, str)
        and attempt_id.isascii()
        and bool(adapter._login_attempt_id)
        and secrets.compare_digest(attempt_id, adapter._login_attempt_id)
    )


async def _finalize_login_locked(adapter):
    session_string = adapter.get_session_string()
    settings = get_settings()

    me = await adapter.client.get_me()
    if not me or type(getattr(me, "id", None)) is not int or me.id <= 0:
        raise RuntimeError("Could not identify the Telegram account")
    username = getattr(me, "username", None)
    if not username:
        username = utils.get_display_name(me)
    else:
        username = f"@{username}"

    # Capture Premium Status
    is_premium = getattr(me, "premium", False)

    repo = get_user_repo()
    existing_user = await repo.get_user(1)

    if existing_user:
        if existing_user.session_string and adapter._expected_login_account_id != me.id:
            raise AccountMismatchError("Telegram account does not match saved account")
        updated = await repo.update_fields(
            existing_user.id,
            {
                "username": username,
                "session_string": session_string,
                "is_premium": is_premium,
            },
            expected_session=existing_user.session_string,
        )
        if not updated:
            raise RuntimeError("Account changed during login")
    else:
        await repo.save_user(
            User(
                id=1,
                api_id=settings.TG_API_ID,
                api_hash=settings.TG_API_HASH,
                username=username,
                session_string=session_string,
                is_premium=is_premium,
            )
        )
    adapter._expected_login_account_id = me.id
    await adapter.activate_login(session_string)
    logger.info("auth_login_completed", username=username, premium=is_premium)

    rule_service = get_rule_service()
    if settings.RULES_SYNC_URL:
        await sync_rules_from_remote(
            settings.RULES_SYNC_URL,
            rule_service.rule_repo,
            repo,
            expected_account_id=getattr(me, "id", None),
            token=getattr(settings, "RULES_SYNC_TOKEN", None),
        )

    # Trigger a startup scan now that we are connected,
    # to catch any unread messages that existed before/during login.
    current_app.managed_tasks.create(rule_service.run_startup_scan(), "startup_scan")
