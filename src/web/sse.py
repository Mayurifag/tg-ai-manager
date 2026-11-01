import asyncio
import json
from dataclasses import asdict

from quart import render_template

from src.domain.models import SystemEvent
from src.web.serializers import json_serializer

# Global state for SSE
connected_queues = set()
shutdown_event = asyncio.Event()


async def broadcast_event(event: SystemEvent):
    """
    Broadcasts a system event to all connected SSE clients.
    Note: This function expects to be running within an application context
    to render templates.
    """
    if event.type == "message" and event.message_model:
        event.rendered_html = await render_template(
            "chat/messages_partial.html.j2",
            messages=[event.message_model],
            chat_id=event.chat_id,
        )

    payload = asdict(event)
    message = payload.get("message_model")
    if message:
        # Telegram document IDs are 64-bit; JavaScript numbers cannot represent
        # every value exactly. Keep emoji IDs as strings in live updates.
        for reaction in message["reactions"]:
            if reaction["custom_emoji_id"] is not None:
                reaction["custom_emoji_id"] = str(reaction["custom_emoji_id"])
    data = json.dumps(payload, default=json_serializer)

    for queue in connected_queues:
        await queue.put(data)
