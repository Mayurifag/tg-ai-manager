import asyncio

from quart import Blueprint, abort, jsonify, render_template, request

from src.application.interactors import ChatSnapshotExpired
from src.domain.errors import LogUnavailable
from src.container import (
    _get_tg_adapter,
    get_chat_interactor,
    get_rule_service,
    get_user_repo,
)
from src.infrastructure.telegram_queue import WriteQueueFull
from src.requests import optional_int
from src.rules.models import RuleType
from src.web.requests import BadRequest, MarkReadRequest, ReactionRequest

chat_bp = Blueprint("chat", __name__)


def bad_request(error: BadRequest):
    return jsonify({"error": str(error)}), 400


def account_id_for_view() -> str | None:
    account_id = getattr(_get_tg_adapter(), "_self_id", None)
    return str(account_id) if type(account_id) is int and account_id > 0 else None


@chat_bp.route("/")
async def index():
    page = await get_chat_interactor().get_chat_page()
    return await render_template("index/index.html.j2", page=page)


@chat_bp.route("/api/chats")
async def api_chats():
    snapshot = request.args.get("snapshot") or None
    raw_offset = request.args.get("offset", "0")
    if (
        len(raw_offset) > 9
        or not raw_offset.isascii()
        or not raw_offset.isdecimal()
        or (snapshot is None and raw_offset != "0")
    ):
        return jsonify({"error": "Invalid chat page offset"}), 400

    try:
        page = await get_chat_interactor().get_chat_page(
            snapshot, int(raw_offset), refresh=request.args.get("refresh") == "1"
        )
    except ChatSnapshotExpired:
        return jsonify({"error": "Chat list expired"}), 410

    html = await render_template("partials/chat_cards.html.j2", chats=page.chats)
    return jsonify(
        {"html": html, "snapshot": page.snapshot, "next_offset": page.next_offset}
    )


@chat_bp.route("/actions")
async def actions_view():
    interactor = get_chat_interactor()
    try:
        logs = await asyncio.wait_for(interactor.get_action_logs(), timeout=2.0)
    except (LogUnavailable, asyncio.TimeoutError):
        return await render_template(
            "actions/log.html.j2", logs=[], logs_unavailable=True
        ), 503
    return await render_template("actions/log.html.j2", logs=logs)


@chat_bp.route("/chat/<int(signed=True):chat_id>")
async def chat_view(chat_id: int):
    interactor = get_chat_interactor()
    rule_service = get_rule_service()
    user_repo = get_user_repo()

    chat, messages = await asyncio.gather(
        interactor.get_chat(chat_id),
        interactor.get_chat_messages(chat_id, topic_id=None),
    )
    if not chat:
        abort(404)

    user = await user_repo.get_user(1)
    is_premium = user.is_premium if user else False

    autoread_rule = await rule_service.get_rule(chat_id, None, RuleType.AUTOREAD)
    ai_autoread_rule = await rule_service.get_rule(chat_id, None, RuleType.AI_AUTOREAD)

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
        account_id=account_id_for_view(),
        autoread_enabled=bool(autoread_rule),
        ai_autoread_enabled=bool(ai_autoread_rule),
        autoreact_status=autoreact_status,
        is_premium=is_premium,
    )


@chat_bp.route("/chat/<int(signed=True):chat_id>/topic/<int(signed=True):topic_id>")
async def topic_view(chat_id: int, topic_id: int):
    interactor = get_chat_interactor()
    rule_service = get_rule_service()
    user_repo = get_user_repo()

    chat, messages = await asyncio.gather(
        interactor.get_chat(chat_id),
        interactor.get_chat_messages(chat_id, topic_id=topic_id),
    )
    if not chat:
        abort(404)
    autoread_rule = await rule_service.get_rule(chat_id, topic_id, RuleType.AUTOREAD)
    ai_autoread_rule = await rule_service.get_rule(
        chat_id, topic_id, RuleType.AI_AUTOREAD
    )

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
        account_id=account_id_for_view(),
        autoread_enabled=bool(autoread_rule),
        ai_autoread_enabled=bool(ai_autoread_rule),
        autoread_inherited=bool(autoread_rule and autoread_rule.topic_id is None),
        ai_autoread_inherited=bool(
            ai_autoread_rule and ai_autoread_rule.topic_id is None
        ),
        autoreact_status=autoreact_status,
        is_premium=is_premium,
    )


@chat_bp.route("/api/chat/<int(signed=True):chat_id>/first-unread")
async def api_first_unread(chat_id: int):
    try:
        topic_id = optional_int(request.args.to_dict(), "topic_id")
    except BadRequest as e:
        return bad_request(e)
    message_id = await get_chat_interactor().first_unread_message_id(chat_id, topic_id)
    return jsonify({"message_id": message_id})


@chat_bp.route("/api/chat/<int(signed=True):chat_id>/history")
async def api_chat_history(chat_id: int):
    interactor = get_chat_interactor()
    modes = [
        key for key in ("offset_id", "around_id", "after_id") if key in request.args
    ]
    if len(modes) > 1 or any(len(request.args.getlist(key)) > 1 for key in modes):
        return jsonify({"error": "Invalid history offset"}), 400
    mode = modes[0] if modes else "offset_id"
    raw_offset = request.args.get(mode, "0")
    if (
        not raw_offset.isascii()
        or not raw_offset.isdecimal()
        or len(raw_offset) > 10
        or int(raw_offset) >= 2**31
        or (mode != "offset_id" and int(raw_offset) == 0)
        or len(request.args.getlist("topic_id")) > 1
    ):
        return jsonify({"error": "Invalid history offset"}), 400
    try:
        topic_id = optional_int(request.args.to_dict(), "topic_id")
    except BadRequest as e:
        return bad_request(e)
    offset = int(raw_offset)
    if mode == "around_id":
        messages = await interactor.get_message_window(chat_id, topic_id, offset)
        if messages is None:
            return jsonify({"error": "Message unavailable in this chat or topic"}), 404
    elif mode == "after_id":
        messages = await interactor.get_chat_messages(
            chat_id, topic_id=topic_id, after_id=offset
        )
    else:
        messages = await interactor.get_chat_messages(
            chat_id, topic_id=topic_id, offset_id=offset
        )
    html_content = await render_template(
        "chat/messages_partial.html.j2", messages=messages, chat_id=chat_id
    )

    return jsonify({"html": html_content, "count": len(messages)})


@chat_bp.route("/api/chat/<int(signed=True):chat_id>/read", methods=["POST"])
async def mark_read(chat_id: int):
    interactor = get_chat_interactor()
    try:
        body = MarkReadRequest.from_json(await request.get_json())
    except BadRequest as e:
        return bad_request(e)

    try:
        await interactor.mark_chat_as_read(chat_id, topic_id=body.topic_id)
    except WriteQueueFull:
        return jsonify(
            {"error": "Telegram is busy; the read was not queued. Try again."}
        ), 503
    return jsonify({"status": "queued"}), 202


@chat_bp.route("/api/chat/<int(signed=True):chat_id>/authors")
async def api_get_authors(chat_id: int):
    interactor = get_chat_interactor()
    authors = await interactor.get_recent_authors(chat_id)
    return jsonify({"authors": authors})


@chat_bp.route("/api/chat/<int(signed=True):chat_id>/card")
async def api_get_chat_card(chat_id: int):
    interactor = get_chat_interactor()
    chat = await interactor.get_chat(chat_id, refresh=True)
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
    try:
        body = ReactionRequest.from_json(await request.get_json())
    except BadRequest as e:
        return bad_request(e)

    try:
        success = await interactor.toggle_reaction(chat_id, msg_id, body.reaction)
    except WriteQueueFull:
        return jsonify(
            {"error": "Telegram is busy; the reaction was not queued. Try again."}
        ), 503
    if success:
        return jsonify({"status": "queued"}), 202
    return jsonify({"error": "Failed to set reaction"}), 500
