from datetime import datetime, timezone

from quart import Blueprint, abort, current_app, render_template, request
from src.container import get_chat_interactor
from src.domain.errors import AccountChanged
from src.domain.models import ForumTopicCursor
from src.infrastructure.tasks import gather_owned
from src.web.requests import BadRequest, query_values

forum_bp = Blueprint("forum", __name__)


@forum_bp.route("/forum/<int(signed=True):chat_id>")
async def forum_view(chat_id: int):
    cursor = None
    names = ("offset_date", "offset_id", "offset_topic")
    try:
        params = query_values(set(names))
    except BadRequest:
        abort(400, description="Invalid forum topic cursor")
    if any(name in params for name in names):
        values = [params.get(name, "") for name in names]
        if any(
            not value.isascii()
            or not value.isdecimal()
            or len(value) > 10
            or not 0 < int(value) < 2**31
            for value in values
        ):
            abort(400, description="Invalid forum topic cursor")
        cursor = ForumTopicCursor(
            datetime.fromtimestamp(int(values[0]), tz=timezone.utc),
            int(values[1]),
            int(values[2]),
        )
    interactor = get_chat_interactor()
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

    async def load_topics():
        check()
        return await interactor.get_forum_topics_page(chat_id, cursor)

    check()
    chat, page = await gather_owned(load_chat, load_topics)
    check()
    if not chat:
        abort(404)
    interactor.prefetch_topics(chat, page.topics)
    next_cursor = page.next_cursor
    result = await render_template(
        "forum/forum.html.j2",
        chats=page.topics,
        chat=chat,
        parent_id=chat_id,
        has_cursor=cursor is not None,
        next_cursor={
            "offset_date": int(next_cursor.date.timestamp()),
            "offset_id": next_cursor.message_id,
            "offset_topic": next_cursor.topic_id,
        }
        if next_cursor
        else None,
    )
    check()
    return result


@forum_bp.route(
    "/api/forum/<int(signed=True):chat_id>/topic/<int(signed=True):topic_id>/card"
)
async def api_get_topic_card(chat_id: int, topic_id: int):
    interactor = get_chat_interactor()
    repository = getattr(request, "_account_adapter", None)
    if repository is None:
        raise AccountChanged()

    def check() -> None:
        interactor._ensure_repository_current(repository)
        if current_app.tg_adapter is not repository:
            raise AccountChanged()

    check()
    topic = await interactor.get_forum_topic(chat_id, topic_id)
    check()
    if not topic:
        return "Topic not found", 404
    result = await render_template(
        "forum/topic_card_partial.html.j2", topic=topic, parent_id=chat_id
    )
    check()
    return result
