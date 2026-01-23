from typing import Any, Optional

from telethon import errors, functions, types, utils

from src.domain.models import SystemEvent
from src.infrastructure.event_bus import EventBus
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class TelegramWriterComponent:
    def __init__(self, client: Any, event_bus: EventBus, reader_component: Any):
        self.client = client
        self.event_bus = event_bus
        self.reader = reader_component

    async def mark_as_read(
        self, chat_id: int, topic_id: Optional[int] = None, max_id: Optional[int] = None
    ):
        try:
            entity = await self.client.get_entity(chat_id)
            chat_name = utils.get_display_name(entity)
            topic_name = None

            if topic_id:
                try:
                    read_max_id = max_id
                    if not read_max_id:
                        msgs = await self.client.get_messages(
                            entity, limit=1, reply_to=topic_id
                        )
                        read_max_id = msgs[0].id if msgs else topic_id

                    await self.client(
                        functions.messages.ReadDiscussionRequest(
                            peer=entity, msg_id=topic_id, read_max_id=read_max_id
                        )
                    )
                    topic_name = await self.reader.get_topic_name(chat_id, topic_id)
                except Exception as e:
                    if "TOPIC_ID_INVALID" in str(e):
                        logger.warning(
                            "mark_read_topic_invalid",
                            chat_id=chat_id,
                            topic_id=topic_id,
                        )
                    else:
                        raise e
            else:
                if max_id:
                    await self.client.send_read_acknowledge(entity, max_id=max_id)
                else:
                    await self.client.send_read_acknowledge(entity)

            await self.event_bus.publish(
                SystemEvent(
                    type="read",
                    text="Marked as read",
                    chat_name=chat_name,
                    topic_name=topic_name,
                    chat_id=chat_id,
                    topic_id=topic_id,
                    is_read=True,
                    link=f"/chat/{chat_id}",
                )
            )

        except Exception as e:
            logger.error("mark_as_read_failed", chat_id=chat_id, error=str(e))

    async def _resolve_reaction(self, emoji: str):
        if emoji.isdigit():
            return types.ReactionCustomEmoji(document_id=int(emoji))
        return types.ReactionEmoji(emoticon=emoji)

    async def send_reaction(self, chat_id: int, msg_id: int, emoji: str) -> bool:
        try:
            entity = await self.client.get_entity(chat_id)
            target_reaction = await self._resolve_reaction(emoji)

            # 1. Send to Main Chat
            success = await self._execute_reaction(entity, msg_id, target_reaction)
            if not success:
                return False

            # 2. Check for Linked Chat Propagation
            # Only if this is a channel (Broadcast), it might have a linked discussion group
            if isinstance(entity, types.Channel) and getattr(
                entity, "broadcast", False
            ):
                try:
                    # Get the discussion message in the linked group
                    discussion_msg = await self.client.get_discussion_message(
                        entity, msg_id
                    )
                    if discussion_msg:
                        # Recursively react to the discussion message
                        # We don't return false if this fails, as the main one succeeded
                        await self._execute_reaction(
                            discussion_msg.chat_id, discussion_msg.id, target_reaction
                        )
                        logger.info(
                            "reaction_propagated_to_discussion",
                            source_chat=chat_id,
                            target_chat=discussion_msg.chat_id,
                        )
                except errors.MsgIdInvalidError:
                    pass  # No discussion linked or message not found
                except Exception as e:
                    logger.warning("reaction_propagation_failed", error=str(e))

            if success:
                # Optimistic Update Event
                # We fetch the message again to get the updated state for the UI
                updated_msgs = await self.client.get_messages(entity, ids=[msg_id])
                if updated_msgs:
                    parsed = await self.reader.parser.parse(
                        updated_msgs[0], chat_id=chat_id
                    )
                    await self.event_bus.publish(
                        SystemEvent(
                            type="reaction_update",
                            text="",
                            chat_name="",
                            chat_id=chat_id,
                            message_model=parsed,
                        )
                    )

            return success

        except Exception as e:
            logger.error(
                "send_reaction_failed", chat_id=chat_id, msg_id=msg_id, error=str(e)
            )
            return False

    async def _execute_reaction(
        self, peer: Any, msg_id: int, target_reaction: Any
    ) -> bool:
        """Internal helper to calculate stack and send reaction."""
        try:
            msgs = await self.client.get_messages(peer, ids=[msg_id])
            if not msgs:
                return False
            msg = msgs[0]

            # Merge with existing reactions
            current_reactions = []
            if hasattr(msg, "reactions") and msg.reactions:
                for rc in msg.reactions.results:
                    if getattr(rc, "chosen", False):
                        current_reactions.append(rc.reaction)

            new_list = []
            found = False
            for r in current_reactions:
                is_same = False
                if isinstance(r, types.ReactionEmoji) and isinstance(
                    target_reaction, types.ReactionEmoji
                ):
                    if r.emoticon == target_reaction.emoticon:
                        is_same = True
                elif isinstance(r, types.ReactionCustomEmoji) and isinstance(
                    target_reaction, types.ReactionCustomEmoji
                ):
                    if r.document_id == target_reaction.document_id:
                        is_same = True

                if is_same:
                    found = True
                else:
                    new_list.append(r)

            if not found:
                new_list.append(target_reaction)

            try:
                await self.client(
                    functions.messages.SendReactionRequest(
                        peer=peer, msg_id=msg_id, reaction=new_list, add_to_recent=True
                    )
                )
                return True
            except errors.ReactionInvalidError:
                # Fallback: Replace all
                fallback = [target_reaction] if not found else []
                await self.client(
                    functions.messages.SendReactionRequest(
                        peer=peer, msg_id=msg_id, reaction=fallback, add_to_recent=True
                    )
                )
                return True
        except Exception as e:
            logger.error("execute_reaction_exception", error=str(e))
            return False
