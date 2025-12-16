import os
import sys
import json
import asyncio
from dataclasses import asdict, is_dataclass
from datetime import datetime
from quart import Quart, render_template, send_from_directory, request, jsonify, make_response, render_template_string, redirect
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

def json_serializer(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    # Check if it's an instance, not a class type
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    raise TypeError(f"Type {type(obj)} not serializable")

async def broadcast_event(event: SystemEvent):
    """Pushes a new event to all connected SSE clients."""

    # Pre-render HTML for messages
    if event.type == "message" and event.message_model:
        # We pass a list of 1 message to reuse the loop template
        # Must pass chat_id for media links
        event.rendered_html = await render_template(
            "chat/messages_partial.html.j2",
            messages=[event.message_model],
            chat_id=event.chat_id
        )
    # Also render for Edited messages so we can replace the bubble text
    elif event.type == "edited" and event.message_model:
        # For edits, we might only need the text content, but keeping it consistent
        # We won't use the full partial for replacement, but having the model data available is key.
        # Use the same template effectively creates a replacement node.
        pass

    # Manual serialization using custom default
    data = json.dumps(asdict(event), default=json_serializer)

    for queue in connected_queues:
        await queue.put(data)

@app.before_serving
async def startup():
    # Reset DB on start
    if os.path.exists("actions.db"):
        try:
            os.remove("actions.db")
            print("Reset actions.db")
        except Exception as e:
            print(f"Failed to delete actions.db: {e}")

    interactor = get_chat_interactor()
    await interactor.initialize()
    await interactor.subscribe_to_events(broadcast_event)

@app.after_serving
async def shutdown():
    interactor = get_chat_interactor()
    # Disconnect Telegram client
    await interactor.shutdown()

    # Gracefully close all SSE connections by sending a kill signal (None)
    put_tasks = [queue.put(None) for queue in list(connected_queues)]
    if put_tasks:
        # Await all put operations to ensure the signal is delivered
        await asyncio.gather(*put_tasks, return_exceptions=True)

    # Give the generators a brief moment to process 'None', exit, and clean up.
    # This short delay often resolves Ctrl+C hang issues in async web servers
    # with open connections.
    await asyncio.sleep(0.5)

    connected_queues.clear()

@app.context_processor
def inject_recent_events():
    interactor = get_chat_interactor()
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
                # Check for kill signal
                if data is None:
                    break
                yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            # Client disconnected manually (e.g. closing tab)
            pass
        finally:
            if queue in connected_queues:
                connected_queues.remove(queue)

    response = await make_response(generator())

    # Avoid Pylance error for dynamic attribute assignment
    setattr(response, 'timeout', None)

    response.headers['Content-Type'] = 'text/event-stream'
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    return response

@app.route("/cache/<path:filename>")
async def serve_images(filename):
    return await send_from_directory(IMAGES_DIR, filename)

@app.route("/media/<int(signed=True):chat_id>/<int(signed=True):msg_id>")
async def get_message_media(chat_id: int, msg_id: int):
    """Lazy load media for a message."""
    interactor = get_chat_interactor()

    # Try to find if file already exists in cache with a pattern (since we don't know extension easily)
    # Actually, the adapter handles the cache logic and returns a path like "/cache/..."

    public_path = await interactor.get_media_path(chat_id, msg_id)
    if public_path:
        return redirect(public_path)

    return "", 404

@app.route("/static/css/<path:filename>")
async def serve_css(filename):
    return await send_from_directory(CSS_DIR, filename)

@app.route("/")
async def index():
    interactor = get_chat_interactor()
    chats = await interactor.get_recent_chats()
    return await render_template("index/index.html.j2", chats=chats)

@app.route("/actions")
async def actions_view():
    interactor = get_chat_interactor()
    logs = await interactor.get_action_logs()
    return await render_template("actions/log.html.j2", logs=logs)

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
    html_content = await render_template("chat/messages_partial.html.j2", messages=messages, chat_id=chat_id)

    return jsonify({
        "html": html_content,
        "count": len(messages)
    })

@app.route("/api/chat/<int(signed=True):chat_id>/read", methods=["POST"])
async def mark_read(chat_id: int):
    interactor = get_chat_interactor()
    data = await request.get_json()
    topic_id = None
    if data:
        topic_id = data.get("topic_id")

    await interactor.mark_chat_as_read(chat_id, topic_id=topic_id)
    return jsonify({"status": "ok"})

@app.route("/api/chat/<int(signed=True):chat_id>/card")
async def api_get_chat_card(chat_id: int):
    """Fetches a rendered HTML card for a specific chat."""
    interactor = get_chat_interactor()
    chat = await interactor.get_chat(chat_id)
    if not chat:
        return "Chat not found", 404

    # Render using the existing macro. We wrap it in a string template to use 'import'.
    template = """
    {% import "macros/chat_card.html.j2" as cards %}
    <div class="chat-card-wrapper" data-chat-id="{{ chat.id }}">
        {{ cards.render_chat_card(chat) }}
    </div>
    """
    return await render_template_string(template, chat=chat)
