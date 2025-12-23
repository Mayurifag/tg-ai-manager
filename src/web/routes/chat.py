from quart import Blueprint, render_template, request, jsonify, abort
from src.container import get_chat_interactor, get_rule_service

chat_bp = Blueprint('chat', __name__)

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
    chat = await interactor.get_chat(chat_id)
    if not chat:
        abort(404)

    # Load initial messages for regular chats (same as topics)
    messages = await interactor.get_chat_messages(chat_id, topic_id=None)

    autoread_enabled = await rule_service.is_autoread_enabled(chat_id)
    return await render_template(
        "chat/chat.html.j2",
        messages=messages,
        chat=chat,
        chat_id=chat_id,
        topic_id=None,
        autoread_enabled=autoread_enabled
    )

@chat_bp.route("/chat/<int(signed=True):chat_id>/topic/<int(signed=True):topic_id>")
async def topic_view(chat_id: int, topic_id: int):
    interactor = get_chat_interactor()
    rule_service = get_rule_service()
    chat = await interactor.get_chat(chat_id)
    messages = await interactor.get_chat_messages(chat_id, topic_id=topic_id)
    autoread_enabled = await rule_service.is_autoread_enabled(chat_id, topic_id)
    return await render_template(
        "chat/chat.html.j2",
        messages=messages,
        chat=chat,
        chat_id=chat_id,
        topic_id=topic_id,
        autoread_enabled=autoread_enabled
    )

@chat_bp.route("/api/chat/<int(signed=True):chat_id>/history")
async def api_chat_history(chat_id: int):
    interactor = get_chat_interactor()
    offset_id = request.args.get('offset_id', type=int, default=0)
    topic_id = request.args.get('topic_id', type=int, default=None)

    messages = await interactor.get_chat_messages(chat_id, topic_id=topic_id, offset_id=offset_id)
    html_content = await render_template("chat/messages_partial.html.j2", messages=messages, chat_id=chat_id)

    return jsonify({
        "html": html_content,
        "count": len(messages)
    })

@chat_bp.route("/api/chat/<int(signed=True):chat_id>/read", methods=["POST"])
async def mark_read(chat_id: int):
    interactor = get_chat_interactor()
    data = await request.get_json()
    topic_id = None
    if data:
        topic_id = data.get("topic_id")

    await interactor.mark_chat_as_read(chat_id, topic_id=topic_id)
    return jsonify({"status": "ok"})

@chat_bp.route("/api/chat/<int(signed=True):chat_id>/card")
async def api_get_chat_card(chat_id: int):
    interactor = get_chat_interactor()
    chat = await interactor.get_chat(chat_id)
    if not chat:
        return "Chat not found", 404

    return await render_template("partials/chat_card_wrapper.html.j2", chat=chat)
