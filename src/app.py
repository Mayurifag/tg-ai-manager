import os
from quart import Quart, render_template, send_from_directory
from src.container import get_chat_service

app = Quart(__name__)

STATIC_DIR = os.path.join(os.getcwd(), "static")
IMAGES_DIR = os.path.join(STATIC_DIR, "images")
os.makedirs(IMAGES_DIR, exist_ok=True)

@app.before_serving
async def startup():
    service = get_chat_service()
    await service.initialize()

@app.after_serving
async def shutdown():
    service = get_chat_service()
    await service.shutdown()

@app.route("/static/images/<path:filename>")
async def serve_images(filename):
    return await send_from_directory(IMAGES_DIR, filename)

@app.route("/")
async def index():
    service = get_chat_service()
    chats = await service.get_recent_chats()
    return await render_template("index.html", chats=chats)

@app.route("/chat/<int(signed=True):chat_id>")
async def chat_view(chat_id: int):
    service = get_chat_service()
    messages = await service.get_chat_messages(chat_id)
    return await render_template("chat.html", messages=messages, chat_id=chat_id, topic_id=None)

@app.route("/forum/<int(signed=True):chat_id>")
async def forum_view(chat_id: int):
    service = get_chat_service()
    topics = await service.get_forum_topics(chat_id)
    return await render_template("forum.html", chats=topics, parent_id=chat_id)

@app.route("/chat/<int(signed=True):chat_id>/topic/<int(signed=True):topic_id>")
async def topic_view(chat_id: int, topic_id: int):
    service = get_chat_service()
    messages = await service.get_chat_messages(chat_id, topic_id=topic_id)
    return await render_template("chat.html", messages=messages, chat_id=chat_id, topic_id=topic_id)
