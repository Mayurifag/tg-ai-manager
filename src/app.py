import os
import sys
import json
import asyncio
from dataclasses import asdict
from quart import Quart, render_template, send_from_directory, request, jsonify, make_response
from src.container import get_chat_interactor
from src.domain.models import SystemEvent

app = Quart(__name__)

STATIC_DIR = os.path.join(os.getcwd(), "static")
IMAGES_DIR = os.path.join(os.getcwd(), "cache")
CSS_DIR = os.path.join(STATIC_DIR, "css")
os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(CSS_DIR, exist_ok=True)

# Connected SSE clients
connected_queues = set()

async def broadcast_event(event: SystemEvent):
    """Pushes a new event to all connected SSE clients."""
    # Serialize datetime manually
    event_dict = asdict(event)
    event_dict['date'] = event.date.isoformat()

    data = json.dumps(event_dict)
    for queue in connected_queues:
        await queue.put(data)

@app.before_serving
async def startup():
    interactor = get_chat_interactor()
    await interactor.initialize()
    # Subscribe to events and broadcast them
    await interactor.subscribe_to_events(broadcast_event)

@app.after_serving
async def shutdown():
    interactor = get_chat_interactor()
    await interactor.shutdown()

@app.context_processor
def inject_recent_events():
    interactor = get_chat_interactor()
    # Return as list for jinja
    return {'recent_events': interactor.get_recent_events()}

@app.route("/api/events/stream")
async def event_stream():
    if "text/event-stream" not in request.accept_mimetypes:
        return "SSE only", 400

    queue = asyncio.Queue()
    connected_queues.add(queue)

    async def generator():
        try:
            while True:
                data = await queue.get()
                yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            connected_queues.remove(queue)
            raise

    response = await make_response(generator())
    response.timeout = None  # Infinite timeout for streaming
    response.headers['Content-Type'] = 'text/event-stream'
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    return response

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
    return await render_template("index/index.html.j2", chats=chats)

@app.route("/chat/<int(signed=True):chat_id>")
async def chat_view(chat_id: int):
    interactor = get_chat_interactor()
    chat = await interactor.get_chat(chat_id)
    messages = await interactor.get_chat_messages(chat_id)
    return await render_template("chat/chat.html.j2", messages=messages, chat=chat, chat_id=chat_id, topic_id=None)

@app.route("/forum/<int(signed=True):chat_id>")
async def forum_view(chat_id: int):
    interactor = get_chat_interactor()
    chat = await interactor.get_chat(chat_id)
    topics = await interactor.get_forum_topics(chat_id)
    return await render_template("forum/forum.html.j2", chats=topics, chat=chat, parent_id=chat_id)

@app.route("/chat/<int(signed=True):chat_id>/topic/<int(signed=True):topic_id>")
async def topic_view(chat_id: int, topic_id: int):
    interactor = get_chat_interactor()
    chat = await interactor.get_chat(chat_id)
    messages = await interactor.get_chat_messages(chat_id, topic_id=topic_id)
    return await render_template("chat/chat.html.j2", messages=messages, chat=chat, chat_id=chat_id, topic_id=topic_id)

@app.route("/api/chat/<int(signed=True):chat_id>/history")
async def api_chat_history(chat_id: int):
    interactor = get_chat_interactor()
    offset_id = request.args.get('offset_id', type=int, default=0)
    topic_id = request.args.get('topic_id', type=int, default=None)

    messages = await interactor.get_chat_messages(chat_id, topic_id=topic_id, offset_id=offset_id)

    html_content = await render_template("chat/messages_partial.html.j2", messages=messages)

    return jsonify({
        "html": html_content,
        "count": len(messages)
    })
