from urllib.parse import urlsplit

from quart import Quart, jsonify, redirect, request, url_for

from src.container import _get_tg_adapter
from src.domain.errors import AccountChanged, TelegramUnavailable
from src.infrastructure.logging import get_logger
from src.infrastructure.security import SecretStorageError
from src.requests import BadRequest, require_int
from src.rules.ports import InvalidRuleConfiguration
from src.web.routes.auth import auth_bp
from src.web.routes.chat import chat_bp
from src.web.routes.forum import forum_bp
from src.web.routes.health import health_bp
from src.web.routes.media import media_bp
from src.web.routes.settings import settings_bp
from src.web.routes.sse import sse_bp

logger = get_logger(__name__)


def register_routes(app: Quart):
    app.register_blueprint(auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(forum_bp)
    app.register_blueprint(media_bp)
    app.register_blueprint(sse_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(health_bp)

    @app.errorhandler(SecretStorageError)
    async def credential_storage_unavailable(_error):
        # The chained provider exception may contain credential values. Quart's
        # default unhandled-error log would include the full traceback.
        logger.error("credential_storage_unavailable")
        message = "Credential storage is unavailable. No change was confirmed."
        if request.path.startswith("/api/"):
            return jsonify({"error": message}), 503
        return message, 503

    @app.errorhandler(TelegramUnavailable)
    async def telegram_unavailable(error):
        status = 429 if error.retry_after is not None else 503
        message = "Telegram is temporarily unavailable. Try again later."
        if request.path.startswith("/api/"):
            response = jsonify({"error": message})
        else:
            response = app.response_class(message, mimetype="text/plain")
        response.status_code = status
        if error.retry_after is not None:
            response.headers["Retry-After"] = str(error.retry_after)
        return response

    @app.errorhandler(AccountChanged)
    async def account_changed(_error):
        message = "The Telegram account changed. Reload and try again."
        if request.path.startswith("/api/"):
            return jsonify({"error": message}), 409
        return message, 409

    @app.errorhandler(InvalidRuleConfiguration)
    async def invalid_rule_configuration(error):
        message = "Stored rule configuration is invalid. Check the saved rules."
        rule_id = error.rule_id
        if type(rule_id) is int and rule_id > 0:
            message = f"Stored rule {rule_id} has invalid configuration."
        if request.path.startswith("/api/"):
            return jsonify({"error": message, "rule_id": rule_id}), 503
        return message, 503

    @app.errorhandler(400)
    async def invalid_http_request(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Invalid request"}), 400
        return error

    @app.errorhandler(408)
    async def request_timed_out(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Request body timed out"}), 408
        return error

    @app.errorhandler(413)
    async def request_too_large(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Request body is too large"}), 413
        return error

    @app.before_request
    async def protect_mutations():
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return
        # Browser cross-origin forms cannot set this header; cross-origin fetch
        # requires a CORS preflight, which this app does not grant.
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            return jsonify({"error": "Same-origin request required"}), 403
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return jsonify({"error": "Same-origin request required"}), 403
        origin = request.headers.get("Origin")
        if origin:
            try:
                parsed = urlsplit(origin)
                host = urlsplit("//" + request.host)
                default_port = 443 if parsed.scheme == "https" else 80
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.path
                    or parsed.query
                    or parsed.fragment
                    or parsed.hostname.lower() != (host.hostname or "").lower()
                    or (parsed.port or default_port) != (host.port or default_port)
                ):
                    raise ValueError("Origin mismatch")
            except ValueError:
                return jsonify({"error": "Same-origin request required"}), 403

    @app.after_request
    async def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
        )
        if not request.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.before_request
    async def login_required():
        if (
            request.path.startswith("/static")
            or request.path.startswith("/api/auth")
            or request.path == "/login"
            or request.path == "/api/health"
            or request.path == "/api/live"
            or request.path == "/api/rules/export"
            or request.path == "/api/settings/reset"
        ):
            return

        adapter = _get_tg_adapter()
        # If not connected, force login
        if not adapter or not adapter.is_connected():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Telegram account is not connected"}), 503
            return redirect(url_for("auth.login_page"))
        request._account_adapter = adapter

    @app.before_request
    async def validate_route_ids():
        values = request.view_args or {}
        try:
            for key in ("chat_id", "topic_id", "msg_id", "doc_id", "rule_id"):
                if key not in values:
                    continue
                value = require_int(values, key)
                if key in {"doc_id", "rule_id"} and value <= 0:
                    raise BadRequest(f"{key} must be positive")
        except BadRequest as error:
            if request.path.startswith("/api/"):
                return jsonify({"error": str(error)}), 400
            return str(error), 400

    @app.before_request
    async def protect_account_mutations():
        if request.method in {"GET", "HEAD", "OPTIONS"} or request.endpoint == (
            "settings.api_debug_process"
        ):
            return
        adapter = getattr(request, "_account_adapter", None)
        if adapter is None:
            # Login and reset own the lifecycle lock themselves.
            return
        # Receive the body before taking the lock so a slow client cannot
        # hold up account retirement while uploading a request.
        await request.get_data(cache=True)
        await app.login_lock.acquire()
        request._account_mutation_lock = True
        if _get_tg_adapter() is not adapter or not adapter.is_connected():
            return jsonify({"error": "Account changed; retry the request"}), 409

    @app.after_request
    async def discard_retired_account_response(response):
        adapter = getattr(request, "_account_adapter", None)
        login_check = getattr(request, "_login_page_check", None)
        if login_check is not None:
            try:
                login_check()
            except AccountChanged:
                pass
            else:
                return response
        else:
            if adapter is None or request.endpoint == "sse.event_stream":
                return response
            if (
                _get_tg_adapter() is adapter
                and getattr(adapter, "_retired", False) is not True
            ):
                return response

        # Quart response bodies may own a document stream lease even before
        # iteration starts. Close a discarded body rather than leaking it.
        body = response.response
        if hasattr(body, "__aenter__") and hasattr(body, "__aexit__"):
            async with body:
                pass
        message = "The Telegram account changed. Reload and try again."
        if request.path.startswith("/api/"):
            replacement = jsonify({"error": message})
            replacement.status_code = 409
            return replacement
        return app.response_class(message, status=409, mimetype="text/plain")

    @app.teardown_request
    async def release_account_mutation_lock(_error):
        if getattr(request, "_account_mutation_lock", False):
            request._account_mutation_lock = False
            app.login_lock.release()
