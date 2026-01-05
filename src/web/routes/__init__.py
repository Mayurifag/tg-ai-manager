from quart import Quart
from src.web.routes.chat import chat_bp
from src.web.routes.forum import forum_bp
from src.web.routes.rules import rules_bp
from src.web.routes.media import media_bp
from src.web.routes.sse import sse_bp
from src.web.routes.settings import settings_bp


def register_routes(app: Quart):
    app.register_blueprint(chat_bp)
    app.register_blueprint(forum_bp)
    app.register_blueprint(rules_bp)
    app.register_blueprint(media_bp)
    app.register_blueprint(sse_bp)
    app.register_blueprint(settings_bp)
