from quart import Blueprint, abort, jsonify, render_template, request

from src.container import get_chat_interactor, get_rule_service, get_user_repo
from src.rules.models import RuleType

chat_bp = Blueprint("chat", __name__)


@chat_bp.route("/")
async def index():
    interactor = get_chat_interactor()
    chats = await interactor.get_recent_chats()
    return await render_template("index/index.html.j2", chats=chats)


@chat_bp.route("/actions")
async def actions_view():
    interactor = get_chat_interactor()
    logs = await interactor.get_action_logs()
    return await render_template("actions/log.html.j2", logs=logs)


@chat_bp.route("/chat/<int(signed=True):chat_id>")
async def chat_view(chat_id: int):
    interactor = get_chat_interactor()
    rule_service = get_rule_service()
    user_repo = get_user_repo()

    chat = await interactor.get_chat(chat_id)
    if not chat:
        abort(404)

    user = await user_repo.get_user(1)
    is_premium = user.is_premium if user else False

    messages = await interactor.get_chat_messages(chat_id, topic_id=None)

    autoread_enabled = await rule_service.is_autoread_enabled(chat_id)

    # Determine Autoreact Status
    react_rule = await rule_service.get_rule(chat_id, None, RuleType.AUTOREACT)
    autoreact_status = "off"
    if react_rule:
        if not react_rule.config.get("target_users"):
            autoreact_status = "all"
        else:
            autoreact_status = "some"

    return await render_template(
        "chat/chat.html.j2",
        messages=messages,
        chat=chat,
        chat_id=chat_id,
        topic_id=None,
        autoread_enabled=autoread_enabled,
        autoreact_status=autoreact_status,
        is_premium=is_premium,
    )


@chat_bp.route("/chat/<int(signed=True):chat_id>/topic/<int(signed=True):topic_id>")
async def topic_view(chat_id: int, topic_id: int):
    interactor = get_chat_interactor()
    rule_service = get_rule_service()
    user_repo = get_user_repo()

    chat = await interactor.get_chat(chat_id)
    messages = await interactor.get_chat_messages(chat_id, topic_id=topic_id)
    autoread_enabled = await rule_service.is_autoread_enabled(chat_id, topic_id)

    user = await user_repo.get_user(1)
    is_premium = user.is_premium if user else False

    # Determine Autoreact Status
    react_rule = await rule_service.get_rule(chat_id, topic_id, RuleType.AUTOREACT)
    autoreact_status = "off"
    if react_rule:
        if not react_rule.config.get("target_users"):
            autoreact_status = "all"
        else:
            autoreact_status = "some"

    return await render_template(
        "chat/chat.html.j2",
        messages=messages,
        chat=chat,
        chat_id=chat_id,
        topic_id=topic_id,
        autoread_enabled=autoread_enabled,
        autoreact_status=autoreact_status,
        is_premium=is_premium,
    )


@chat_bp.route("/api/chat/<int(signed=True):chat_id>/history")
async def api_chat_history(chat_id: int):
    interactor = get_chat_interactor()
    offset_id = request.args.get("offset_id", type=int, default=0)
    topic_id = request.args.get("topic_id", type=int, default=None)

    messages = await interactor.get_chat_messages(
        chat_id, topic_id=topic_id, offset_id=offset_id
    )
    html_content = await render_template(
        "chat/messages_partial.html.j2", messages=messages, chat_id=chat_id
    )

    return jsonify({"html": html_content, "count": len(messages)})


@chat_bp.route("/api/chat/<int(signed=True):chat_id>/read", methods=["POST"])
async def mark_read(chat_id: int):
    interactor = get_chat_interactor()
    data = await request.get_json()
    topic_id = None
    if data:
        topic_id = data.get("topic_id")

    await interactor.mark_chat_as_read(chat_id, topic_id=topic_id)
    return jsonify({"status": "ok"})


@chat_bp.route("/api/chat/<int(signed=True):chat_id>/authors")
async def api_get_authors(chat_id: int):
    interactor = get_chat_interactor()
    authors = await interactor.get_recent_authors(chat_id)
    return jsonify({"authors": authors})


@chat_bp.route("/api/chat/<int(signed=True):chat_id>/card")
async def api_get_chat_card(chat_id: int):
    interactor = get_chat_interactor()
    chat = await interactor.get_chat(chat_id)
    if not chat:
        return "Chat not found", 404

    return await render_template("partials/chat_card_wrapper.html.j2", chat=chat)


@chat_bp.route("/api/chat/<int(signed=True):chat_id>/info")
async def api_get_chat_info(chat_id: int):
    interactor = get_chat_interactor()
    chat = await interactor.get_chat(chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404

    return jsonify(
        {
            "id": chat.id,
            "name": chat.name,
            "type": chat.type.value,
            "avatar_url": chat.image_url,
        }
    )


@chat_bp.route(
    "/api/chat/<int(signed=True):chat_id>/message/<int(signed=True):msg_id>/reaction",
    methods=["POST"],
)
async def toggle_reaction(chat_id: int, msg_id: int):
    interactor = get_chat_interactor()
    data = await request.get_json()
    emoji = data.get("reaction")

    if not emoji:
        return jsonify({"error": "No reaction provided"}), 400

    success = await interactor.toggle_reaction(chat_id, msg_id, emoji)
    if success:
        return jsonify({"status": "ok"})
    return jsonify({"error": "Failed to set reaction"}), 500
