import asyncio

from quart import Blueprint, abort, current_app, jsonify, render_template, request

from src.application.interactors import ChatSnapshotExpired
from src.domain.errors import AccountChanged, LogUnavailable, ReactionBlocked
from src.container import (
    get_chat_interactor,
    get_rule_service,
    get_user_repo,
)
from src.infrastructure.telegram_queue import WriteQueueFull
from src.infrastructure.tasks import gather_owned, run_with_task_deadline
from src.requests import optional_int
from src.rules.models import RuleType
from src.web.requests import BadRequest, MarkReadRequest, ReactionRequest, query_values

chat_bp = Blueprint("chat", __name__)
ACTION_HISTORY_TIMEOUT = 2.0


def bad_request(error: BadRequest):
    return jsonify({"error": str(error)}), 400


@chat_bp.route("/")
async def index():
    return await _chat_list_page()


async def _chat_list_page(
    *,
    snapshot: str | None = None,
    offset: int = 0,
    refresh: bool = False,
    fragment: bool = False,
):
    interactor = get_chat_interactor()
    repository = getattr(request, "_account_adapter", None)
    if repository is None:
        raise AccountChanged()

    def check() -> None:
        interactor._ensure_repository_current(repository)
        if current_app.tg_adapter is not repository:
            raise AccountChanged()

    check()
    try:
        page = await interactor.get_chat_page(
            snapshot, offset, refresh=refresh, check_current=check
        )
    except ChatSnapshotExpired:
        check()
        return jsonify({"error": "Chat list expired"}), 410
    check()
    if fragment:
        html = await render_template("partials/chat_cards.html.j2", chats=page.chats)
        check()
        return jsonify(
            {"html": html, "snapshot": page.snapshot, "next_offset": page.next_offset}
        )
    result = await render_template("index/index.html.j2", page=page)
    check()
    return result


@chat_bp.route("/api/chats")
async def api_chats():
    try:
        params = query_values({"snapshot", "offset", "refresh"})
    except BadRequest as error:
        return bad_request(error)
    snapshot = params.get("snapshot") or None
    raw_offset = params.get("offset", "0")
    refresh = params.get("refresh", "0")
    if (
        len(raw_offset) > 9
        or not raw_offset.isascii()
        or not raw_offset.isdecimal()
        or (snapshot is None and raw_offset != "0")
        or refresh not in {"0", "1"}
        or (refresh == "1" and snapshot is not None)
        or (snapshot is not None and len(snapshot) > 128)
    ):
        return jsonify({"error": "Invalid chat page parameters"}), 400

    return await _chat_list_page(
        snapshot=snapshot,
        offset=int(raw_offset),
        refresh=refresh == "1",
        fragment=True,
    )


@chat_bp.route("/actions")
async def actions_view():
    interactor = get_chat_interactor()
    repository = getattr(request, "_account_adapter", None)
    if repository is None:
        raise AccountChanged()

    def check() -> None:
        interactor._ensure_repository_current(repository)
        if current_app.tg_adapter is not repository:
            raise AccountChanged()

    async def load_actions():
        try:
            check()
            return await interactor.get_action_logs()
        finally:
            check()

    deadline = asyncio.get_running_loop().time() + ACTION_HISTORY_TIMEOUT
    try:
        logs = await run_with_task_deadline(
            load_actions, deadline, name="action_history_lookup"
        )
    except (LogUnavailable, TimeoutError):
        check()
        result = (
            await render_template(
                "actions/log.html.j2", logs=[], logs_unavailable=True
            ),
            503,
        )
    else:
        check()
        result = await render_template("actions/log.html.j2", logs=logs)
    check()
    return result


@chat_bp.route("/chat/<int(signed=True):chat_id>")
async def chat_view(chat_id: int):
    return await _chat_page(chat_id, None)


@chat_bp.route("/chat/<int(signed=True):chat_id>/topic/<int(signed=True):topic_id>")
async def topic_view(chat_id: int, topic_id: int):
    return await _chat_page(chat_id, topic_id)


async def _chat_page(chat_id: int, topic_id: int | None):
    interactor = get_chat_interactor()
    rule_service = get_rule_service()
    user_repo = get_user_repo()
    repository = getattr(request, "_account_adapter", None)
    if repository is None:
        raise AccountChanged()

    def check() -> None:
        interactor._ensure_repository_current(repository)
        if current_app.tg_adapter is not repository:
            raise AccountChanged()

    async def load_chat():
        check()
        return await interactor.get_chat(chat_id)

    async def load_messages():
        check()
        return await interactor.get_chat_messages(chat_id, topic_id=topic_id)

    check()
    chat, messages = await gather_owned(load_chat, load_messages)
    check()
    if not chat:
        abort(404)

    user = await user_repo.get_user(1)
    check()
    autoread_rule = await rule_service.get_rule(chat_id, topic_id, RuleType.AUTOREAD)
    check()
    ai_autoread_rule = await rule_service.get_rule(
        chat_id, topic_id, RuleType.AI_AUTOREAD
    )
    check()
    react_rule = await rule_service.get_rule(chat_id, topic_id, RuleType.AUTOREACT)
    check()

    autoreact_status = "off"
    if react_rule:
        autoreact_status = "some" if react_rule.config.get("target_users") else "all"

    result = await render_template(
        "chat/chat.html.j2",
        messages=messages,
        chat=chat,
        chat_id=chat_id,
        topic_id=topic_id,
        autoread_enabled=bool(autoread_rule),
        ai_autoread_enabled=bool(ai_autoread_rule),
        autoread_inherited=bool(
            topic_id and autoread_rule and autoread_rule.topic_id is None
        ),
        ai_autoread_inherited=bool(
            topic_id and ai_autoread_rule and ai_autoread_rule.topic_id is None
        ),
        autoreact_status=autoreact_status,
        is_premium=user.is_premium if user else False,
    )
    check()
    return result


@chat_bp.route("/api/chat/<int(signed=True):chat_id>/history")
async def api_chat_history(chat_id: int):
    interactor = get_chat_interactor()
    raw_offset = request.args.get("offset_id", "0")
    if (
        not set(request.args).issubset({"offset_id", "topic_id"})
        or len(request.args.getlist("offset_id")) > 1
        or len(request.args.getlist("topic_id")) > 1
        or not raw_offset.isascii()
        or not raw_offset.isdecimal()
        or len(raw_offset) > 10
        or int(raw_offset) >= 2**31
    ):
        return jsonify({"error": "Invalid history offset"}), 400
    try:
        topic_id = optional_int(request.args.to_dict(), "topic_id")
    except BadRequest as e:
        return bad_request(e)

    repository = getattr(request, "_account_adapter", None)
    if repository is None:
        raise AccountChanged()

    def check() -> None:
        interactor._ensure_repository_current(repository)
        if current_app.tg_adapter is not repository:
            raise AccountChanged()

    check()
    messages = await interactor.get_chat_messages(
        chat_id, topic_id=topic_id, offset_id=int(raw_offset)
    )
    check()
    html_content = await render_template(
        "chat/messages_partial.html.j2", messages=messages, chat_id=chat_id
    )
    check()
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
    repository = getattr(request, "_account_adapter", None)
    if repository is None:
        raise AccountChanged()

    def check() -> None:
        interactor._ensure_repository_current(repository)
        if current_app.tg_adapter is not repository:
            raise AccountChanged()

    check()
    chat = await interactor.get_chat(chat_id, refresh=True)
    check()
    if not chat:
        return "Chat not found", 404
    result = await render_template("partials/chat_card_wrapper.html.j2", chat=chat)
    check()
    return result


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
    except ReactionBlocked as error:
        return jsonify({"error": str(error)}), 409
    except WriteQueueFull:
        return jsonify(
            {"error": "Telegram is busy; the reaction was not queued. Try again."}
        ), 503
    if success:
        return jsonify({"status": "queued"}), 202
    return jsonify({"error": "Failed to set reaction"}), 500
