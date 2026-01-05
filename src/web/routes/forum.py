from quart import Blueprint, render_template
from src.container import get_chat_interactor

forum_bp = Blueprint("forum", __name__)


@forum_bp.route("/forum/<int(signed=True):chat_id>")
async def forum_view(chat_id: int):
    interactor = get_chat_interactor()
    chat = await interactor.get_chat(chat_id)
    topics = await interactor.get_forum_topics(chat_id)
    return await render_template(
        "forum/forum.html.j2", chats=topics, chat=chat, parent_id=chat_id
    )


@forum_bp.route(
    "/api/forum/<int(signed=True):chat_id>/topic/<int(signed=True):topic_id>/card"
)
async def api_get_topic_card(chat_id: int, topic_id: int):
    interactor = get_chat_interactor()
    topics = await interactor.get_forum_topics(chat_id)
    topic = next((t for t in topics if t.id == topic_id), None)
    if not topic:
        return "Topic not found", 404

    return await render_template(
        "forum/topic_card_partial.html.j2", topic=topic, parent_id=chat_id
    )
