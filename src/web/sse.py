import asyncio
import json
from dataclasses import asdict

from quart import render_template

from src.container import get_rule_service
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
    rule_service = get_rule_service()
    await rule_service.handle_new_message_event(event)

    if event.type == "message" and event.message_model:
        event.rendered_html = await render_template(
            "chat/messages_partial.html.j2",
            messages=[event.message_model],
            chat_id=event.chat_id,
        )

    # Handle Reaction Updates: We render a small snippet for the reaction container
    if event.type == "reaction_update" and event.message_model:
        # We wrap the reactions in a dictionary or just pass the message
        # But we need a dedicated partial or logic on frontend.
        # Easier: Render the 'reaction_container' macro/snippet.
        # Since I don't have a macro for it yet, I will inline the HTML generation
        # or create a temporary template context.
        # Ideally, we return the raw list of reactions in JSON and let Frontend JS handle it,
        # but to keep it consistent with "rendered_html", let's render the reactions block.
        pass

    data = json.dumps(asdict(event), default=json_serializer)

    for queue in connected_queues:
        await queue.put(data)
