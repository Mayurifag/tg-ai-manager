import asyncio
import secrets
import sqlite3

from quart import Blueprint, current_app, jsonify, redirect, render_template, request
from telethon import utils
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    ServerError,
    TimedOutError,
)

from src.adapters.telegram.query_errors import unavailable
from src.config import get_settings
from src.container import (
    _get_tg_adapter,
    account_transition,
    get_rule_service,
    get_user_repo,
    reload_tg_adapter,
)
from src.domain.errors import AccountChanged, TelegramUnavailable
from src.infrastructure.logging import get_logger
from src.infrastructure.security import SecretStorageError
from src.infrastructure.tasks import (
    raise_if_cancelled,
    raise_if_task_stopped,
    run_with_task_deadline,
)
from src.rules.sync import sync_rules_from_remote
from src.users.models import User
from src.web.requests import (
    BadRequest,
    PasswordRequest,
    optional_empty_json_body,
    query_values,
)

logger = get_logger(__name__)
auth_bp = Blueprint("auth", __name__)
# shortcut: omit a stalled optional hint after 10 seconds; revisit if Telegram
# routinely needs longer to provide it.
PASSWORD_HINT_TIMEOUT = 10.0


def _account_mismatch_response():
    return jsonify(
        {
            "error": "This is a different Telegram account. Forget the saved account before switching."
        }
    ), 409


def _account_storage_unavailable(error: sqlite3.Error):
    logger.error("auth_storage_unavailable", category=type(error).__name__)
    return jsonify(
        {"error": "Account storage is unavailable. Check before retrying."}
    ), 503


@auth_bp.route("/login", methods=["GET"])
async def login_page():
    app = current_app._get_current_object()  # noqa: SLF001
    adapter = _get_tg_adapter()
    revision = app._account_transition_revision

    def adapter_state():
        return (
            getattr(adapter, "_retired", False),
            getattr(adapter, "_source_session_string", None),
            getattr(adapter, "_login_attempt_id", None),
            getattr(adapter, "_login_finalized", False),
            adapter.is_connected(),
        )

    state = adapter_state()

    def check() -> None:
        raise_if_task_stopped()
        if (
            app.shutdown_event.is_set()
            or app._account_transition_depth
            or app._account_transition_revision != revision
            or app.tg_adapter is not adapter
            or adapter_state() != state
        ):
            raise AccountChanged()

    # The global template context and after-request hook share this snapshot.
    # A retired starting adapter is allowed so same-account recovery still works.
    request._login_page_check = check

    async def render_page():
        try:
            check()
            user = await get_user_repo().get_user(1)
            check()
            if user and user.is_authenticated() and state[-1]:
                return redirect("/")
            return await render_template("auth/login.html.j2")
        finally:
            check()

    return await run_with_task_deadline(render_page, None, name="login_page_view")


@auth_bp.route("/api/auth/qr/start", methods=["POST"])
async def qr_start():
    try:
        await optional_empty_json_body()
    except BadRequest as error:
        return jsonify({"error": str(error)}), 400
    try:
        async with current_app.login_lock:
            if current_app.shutdown_event.is_set():
                return jsonify({"error": "Application is shutting down"}), 503
            user = await run_with_task_deadline(
                lambda: get_user_repo().get_user(1), None, name="qr_saved_account"
            )
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
            _ensure_login_active(adapter)
            adapter._expected_login_account_id = expected_account_id
            adapter._login_attempt_id = secrets.token_urlsafe(24)

            async def start_qr_login() -> str:
                _ensure_login_active(adapter)
                return await adapter.start_qr_login(
                    is_current=lambda: _login_is_active(adapter)
                )

            url = await run_with_task_deadline(
                start_qr_login, None, name="qr_login_start"
            )
            _ensure_login_active(adapter)
            if url == "authorized":
                await _finish_login_locked(adapter)
                return jsonify({"status": "authorized"})

            return jsonify(
                {"status": "ok", "url": url, "attempt_id": adapter._login_attempt_id}
            )
    except AccountMismatchError:
        return _account_mismatch_response()
    except FloodWaitError as error:
        raise unavailable(error) from error
    except (TelegramUnavailable, SecretStorageError):
        raise
    except sqlite3.Error as error:
        return _account_storage_unavailable(error)
    except (OSError, ServerError, TimedOutError) as error:
        logger.warning("qr_start_unavailable", category=type(error).__name__)
        raise unavailable(error) from error
    except Exception as e:
        logger.error("qr_start_failed", category=type(e).__name__)
        return jsonify({"error": "Could not start login"}), 400
    finally:
        raise_if_cancelled()


@auth_bp.route("/api/auth/qr/status", methods=["GET"])
async def qr_status():
    try:
        params = query_values({"attempt_id"})
    except BadRequest:
        return jsonify({"error": "Invalid login parameters"}), 400
    async with current_app.login_lock:
        adapter = _get_tg_adapter()
        if not _matches_attempt(adapter, params.get("attempt_id")):
            return jsonify({"error": "Login attempt was superseded"}), 409
        status = adapter.get_qr_status()
        if status == "authorized" and request.method != "HEAD":
            try:
                if not adapter._login_finalized:
                    await _finish_login_locked(adapter)
            except AccountMismatchError:
                return _account_mismatch_response()
            except FloodWaitError as error:
                raise unavailable(error) from error
            except (TelegramUnavailable, SecretStorageError):
                raise
            except sqlite3.Error as error:
                return _account_storage_unavailable(error)
            except (OSError, ServerError, TimedOutError) as error:
                logger.warning(
                    "auth_finalize_unavailable", category=type(error).__name__
                )
                raise unavailable(error) from error
            except Exception as exc:
                logger.error("auth_finalize_failed", category=type(exc).__name__)
                return jsonify({"error": "Could not complete login"}), 400
            finally:
                raise_if_cancelled()
        return jsonify({"status": status})


@auth_bp.route("/api/auth/hint", methods=["GET"])
async def get_password_hint():
    try:
        params = query_values({"attempt_id"})
    except BadRequest:
        return jsonify({"error": "Invalid login parameters"}), 400
    async with current_app.login_lock:
        adapter = _get_tg_adapter()
        if not _matches_attempt(adapter, params.get("attempt_id")):
            return jsonify({"error": "Login attempt was superseded"}), 409
        deadline = asyncio.get_running_loop().time() + PASSWORD_HINT_TIMEOUT
        try:
            hint = await run_with_task_deadline(
                adapter.get_password_hint, deadline, name="password_hint_lookup"
            )
        except TimeoutError:
            logger.warning("password_hint_timed_out")
            hint = ""
        raise_if_cancelled()
        if not _login_is_active(adapter):
            return jsonify({"error": "Login attempt was superseded"}), 409
        return jsonify({"hint": hint})


@auth_bp.route("/api/auth/2fa", methods=["POST"])
async def login_2fa():
    try:
        data = await request.get_json()
        body = PasswordRequest.from_json(data)
    except BadRequest as e:
        return jsonify({"error": str(e)}), 400

    try:
        async with current_app.login_lock:
            adapter = _get_tg_adapter()
            if not _matches_attempt(adapter, data.get("attempt_id")):
                return jsonify({"error": "Login attempt was superseded"}), 409
            if not adapter._login_finalized:
                if adapter.get_qr_status() != "authorized":

                    async def sign_in() -> None:
                        _ensure_login_active(adapter)
                        await adapter.sign_in_with_password(
                            body.password, is_current=lambda: _login_is_active(adapter)
                        )

                    await run_with_task_deadline(sign_in, None, name="password_sign_in")
                await _finish_login_locked(adapter)
        return jsonify({"status": "ok"})
    except PasswordHashInvalidError:
        return jsonify({"error": "Incorrect password"}), 400
    except AccountMismatchError:
        return _account_mismatch_response()
    except FloodWaitError as error:
        raise unavailable(error) from error
    except (TelegramUnavailable, SecretStorageError):
        raise
    except sqlite3.Error as error:
        return _account_storage_unavailable(error)
    except (OSError, ServerError, TimedOutError) as error:
        logger.warning("auth_2fa_unavailable", category=type(error).__name__)
        raise unavailable(error) from error
    except Exception as e:
        logger.error("auth_2fa_failed", category=type(e).__name__)
        return jsonify({"error": "Could not complete login"}), 400
    finally:
        raise_if_cancelled()


class AccountMismatchError(RuntimeError):
    pass


def _matches_attempt(adapter, attempt_id) -> bool:
    return (
        adapter is not None
        and _login_is_active(adapter)
        and isinstance(attempt_id, str)
        and attempt_id.isascii()
        and bool(adapter._login_attempt_id)
        and secrets.compare_digest(attempt_id, adapter._login_attempt_id)
    )


def _ensure_login_active(adapter) -> None:
    raise_if_task_stopped()
    if not _login_is_active(adapter):
        raise RuntimeError("Login attempt is no longer active")


def _login_is_active(adapter) -> bool:
    return (
        not current_app.shutdown_event.is_set()
        and _get_tg_adapter() is adapter
        and not getattr(adapter, "_retired", False)
    )


async def _finish_login_locked(adapter) -> None:
    async def finish() -> None:
        try:
            await _finalize_login_locked(adapter)
            _ensure_login_active(adapter)
            adapter._login_finalized = True
        finally:
            _ensure_login_active(adapter)

    with account_transition():
        await run_with_task_deadline(finish, None, name="login_finalization")


async def _finalize_login_locked(adapter):
    _ensure_login_active(adapter)
    session_string = adapter.get_session_string()
    settings = get_settings()

    me = await adapter.client.get_me()
    _ensure_login_active(adapter)
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
    _ensure_login_active(adapter)

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
        await repo.create_user(
            User(
                id=1,
                api_id=settings.TG_API_ID,
                api_hash=settings.TG_API_HASH,
                username=username,
                session_string=session_string,
                is_premium=is_premium,
            )
        )
    _ensure_login_active(adapter)
    adapter._expected_login_account_id = me.id
    await adapter.activate_login(
        session_string, is_current=lambda: _login_is_active(adapter)
    )
    _ensure_login_active(adapter)
    logger.info("auth_login_completed", premium=is_premium)

    rule_service = get_rule_service()
    if settings.RULES_SYNC_URL:

        def sync_is_current() -> bool:
            raise_if_task_stopped()
            return _login_is_active(adapter)

        await sync_rules_from_remote(
            settings.RULES_SYNC_URL,
            rule_service.rule_repo,
            repo,
            expected_account_id=getattr(me, "id", None),
            token=getattr(settings, "RULES_SYNC_TOKEN", None),
            ai_policy_change=rule_service.ai_policy_change,
            is_current=sync_is_current,
        )
        _ensure_login_active(adapter)

    # Trigger a startup scan now that we are connected,
    # to catch any unread messages that existed before/during login.
    current_app.managed_tasks.create(
        rule_service.run_startup_scan(adapter), "startup_scan"
    )
