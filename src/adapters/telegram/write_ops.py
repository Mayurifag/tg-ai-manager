import traceback
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Optional

from telethon import errors, functions, types

from src.adapters.telegram.read_ops import ReadOps
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
        self._read_ops = ReadOps(
            client=client,
            write_queue=write_queue,
            dispatch_fn=dispatch_fn,
            get_topic_name_fn=get_topic_name_fn,
        )

    def set_dispatch_fn(
        self, dispatch_fn: Optional[Callable[[SystemEvent], Awaitable[None]]]
    ) -> None:
        self._dispatch_fn = dispatch_fn
        self._read_ops.set_dispatch_fn(dispatch_fn)

    async def mark_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int] = None,
        max_id: Optional[int] = None,
    ) -> None:
        await self._read_ops.mark_as_read(chat_id, topic_id, max_id)

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
