from quart import Quart, redirect, request, url_for

from src.container import _get_tg_adapter
from src.web.routes.auth import auth_bp
from src.web.routes.chat import chat_bp
from src.web.routes.forum import forum_bp
from src.web.routes.health import health_bp
from src.web.routes.media import media_bp
from src.web.routes.queue import queue_bp
from src.web.routes.settings import settings_bp
from src.web.routes.sse import sse_bp


def register_routes(app: Quart):
    app.register_blueprint(auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(forum_bp)
    app.register_blueprint(media_bp)
    app.register_blueprint(sse_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(queue_bp)
    app.register_blueprint(health_bp)

    @app.before_request
    async def login_required():
        if (
            request.path.startswith("/static")
            or request.path.startswith("/api/auth")
            or request.path == "/login"
            or request.path == "/health"
        ):
            return

        adapter = _get_tg_adapter()
        if not adapter or not adapter.is_connected():
            return redirect(url_for("auth.login_page"))
