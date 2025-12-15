import os
from quart import Quart, render_template, send_from_directory
from src.container import get_chat_interactor

app = Quart(__name__)

STATIC_DIR = os.path.join(os.getcwd(), "static")
IMAGES_DIR = os.path.join(os.getcwd(), "cache")
CSS_DIR = os.path.join(STATIC_DIR, "css")
os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(CSS_DIR, exist_ok=True)

@app.before_serving
async def startup():
    interactor = get_chat_interactor()
    await interactor.initialize()

@app.after_serving
async def shutdown():
    interactor = get_chat_interactor()
    await interactor.shutdown()

@app.route("/cache/<path:filename>")
async def serve_images(filename):
    return await send_from_directory(IMAGES_DIR, filename)

@app.route("/static/css/<path:filename>")
async def serve_css(filename):
    return await send_from_directory(CSS_DIR, filename)

@app.route("/")
async def index():
    interactor = get_chat_interactor()
    chats = await interactor.get_recent_chats()
    return await render_template("index/index.html", chats=chats)

@app.route("/chat/<int(signed=True):chat_id>")
async def chat_view(chat_id: int):
    interactor = get_chat_interactor()
    messages = await interactor.get_chat_messages(chat_id)
    return await render_template("chat/chat.html", messages=messages, chat_id=chat_id, topic_id=None)

@app.route("/forum/<int(signed=True):chat_id>")
async def forum_view(chat_id: int):
    interactor = get_chat_interactor()
    topics = await interactor.get_forum_topics(chat_id)
    return await render_template("forum/forum.html", chats=topics, parent_id=chat_id)

@app.route("/chat/<int(signed=True):chat_id>/topic/<int(signed=True):topic_id>")
async def topic_view(chat_id: int, topic_id: int):
    interactor = get_chat_interactor()
    messages = await interactor.get_chat_messages(chat_id, topic_id=topic_id)
    return await render_template("chat/chat.html", messages=messages, chat_id=chat_id, topic_id=topic_id)
