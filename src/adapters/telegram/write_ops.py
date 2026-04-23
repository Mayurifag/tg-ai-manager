import traceback
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Optional

from telethon import errors, functions, types, utils

from src.domain.models import SystemEvent
from src.infrastructure.logging import get_logger

if TYPE_CHECKING:
    from src.adapters.telegram.message_parser import MessageParser

logger = get_logger(__name__)


class WriteOps:
    def __init__(
        self,
        client: Any,
        parser: "MessageParser",
        write_queue: Any,
        dispatch_fn: Optional[Callable[[SystemEvent], Awaitable[None]]],
        get_topic_name_fn: Callable[[int, int], Awaitable[Optional[str]]],
    ) -> None:
        self.client = client
        self._parser = parser
        self._write_queue = write_queue
        self._dispatch_fn = dispatch_fn  # patched after EventHandlers is built
        self._get_topic_name_fn = get_topic_name_fn

    async def mark_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int] = None,
        max_id: Optional[int] = None,
    ) -> None:
        async def _do() -> None:
            try:
                input_peer = await self.client.get_input_entity(chat_id)
                topic_name = None

                if topic_id:
                    try:
                        read_max_id = max_id or topic_id
                        await self.client(
                            functions.messages.ReadDiscussionRequest(
                                peer=input_peer,
                                msg_id=topic_id,
                                read_max_id=read_max_id,
                            )
                        )
                        topic_name = await self._get_topic_name_fn(chat_id, topic_id)
                    except Exception as e:
                        if "TOPIC_ID_INVALID" in str(e):
                            logger.warning(
                                "mark_read_topic_invalid",
                                chat_id=chat_id,
                                topic_id=topic_id,
                            )
                        else:
                            raise
                else:
                    if max_id:
                        await self.client.send_read_acknowledge(
                            input_peer, max_id=max_id
                        )
                    else:
                        await self.client.send_read_acknowledge(input_peer)

                chat_name = f"Chat {chat_id}"
                try:
                    entity = await self.client.get_entity(input_peer)
                    chat_name = utils.get_display_name(entity)
                except Exception:
                    pass

                event = SystemEvent(
                    type="read",
                    text="Marked as read",
                    chat_name=chat_name,
                    topic_name=topic_name,
                    chat_id=chat_id,
                    topic_id=topic_id,
                    is_read=True,
                    link=f"/chat/{chat_id}",
                )
                if self._dispatch_fn:
                    await self._dispatch_fn(event)

            except errors.FloodWaitError:
                raise
            except Exception as e:
                logger.error(
                    "mark_as_read_failed",
                    chat_id=chat_id,
                    topic_id=topic_id,
                    error=repr(e),
                    traceback=traceback.format_exc(),
                )

        await self._write_queue.enqueue(_do)

    async def send_reaction(self, chat_id: int, msg_id: int, emoji: str) -> bool:
        async def _do() -> None:
            try:
                entity = await self.client.get_entity(chat_id)

                target_reaction = None
                if emoji.isdigit():
                    target_reaction = types.ReactionCustomEmoji(document_id=int(emoji))
                else:
                    target_reaction = types.ReactionEmoji(emoticon=emoji)

                msgs = await self.client.get_messages(entity, ids=[msg_id])
                if not msgs:
                    return
                msg = msgs[0]

                current_my_reactions = []
                if hasattr(msg, "reactions") and msg.reactions:
                    for rc in msg.reactions.results:
                        if getattr(rc, "chosen", False):
                            current_my_reactions.append(rc.reaction)

                new_reactions_list = []
                found = False

                for r in current_my_reactions:
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
                        new_reactions_list.append(r)

                if not found:
                    new_reactions_list.append(target_reaction)

                success = False
                try:
                    await self.client(
                        functions.messages.SendReactionRequest(
                            peer=entity,
                            msg_id=msg_id,
                            reaction=new_reactions_list,  # type: ignore
                            add_to_recent=True,
                        )
                    )
                    success = True
                except errors.ReactionInvalidError:
                    logger.info(
                        "reaction_stack_failed_fallback_replace",
                        chat_id=chat_id,
                        msg_id=msg_id,
                    )
                    fallback_list = []
                    if not found:
                        fallback_list = [target_reaction]

                    await self.client(
                        functions.messages.SendReactionRequest(
                            peer=entity,
                            msg_id=msg_id,
                            reaction=fallback_list,  # type: ignore
                            add_to_recent=True,
                        )
                    )
                    success = True
                except Exception as e:
                    logger.error("send_reaction_exception", error=str(e))

                if success:
                    try:
                        updated_msgs = await self.client.get_messages(
                            entity, ids=[msg_id]
                        )
                        if updated_msgs:
                            updated_msg = updated_msgs[0]
                            parsed_msg = await self._parser._parse_message(
                                updated_msg, chat_id=chat_id
                            )
                            event = SystemEvent(
                                type="reaction_update",
                                text="",
                                chat_name="",
                                chat_id=chat_id,
                                message_model=parsed_msg,
                            )
                            if self._dispatch_fn:
                                await self._dispatch_fn(event)
                    except Exception as ex:
                        logger.error(
                            "post_reaction_fetch_failed",
                            error=repr(ex),
                            traceback=traceback.format_exc(),
                        )

            except errors.FloodWaitError:
                raise
            except Exception as e:
                logger.error(
                    "send_reaction_failed",
                    error=str(e),
                    chat_id=chat_id,
                    msg_id=msg_id,
                )

        await self._write_queue.enqueue(_do)
        return True
