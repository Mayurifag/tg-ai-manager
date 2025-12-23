import asyncio
import json
from dataclasses import asdict
from quart import render_template
from src.domain.models import SystemEvent
from src.container import get_rule_service
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
    rule_service = get_rule_service()
    await rule_service.handle_new_message_event(event)

    if event.type == "message" and event.message_model:
        event.rendered_html = await render_template(
            "chat/messages_partial.html.j2",
            messages=[event.message_model],
            chat_id=event.chat_id
        )

    data = json.dumps(asdict(event), default=json_serializer)

    for queue in connected_queues:
        await queue.put(data)
