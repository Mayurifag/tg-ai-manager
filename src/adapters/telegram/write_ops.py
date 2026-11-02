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
        self._pending_automatic_reactions: set[tuple[int, int, str]] = set()
        self._closed = False
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

    async def close(self) -> None:
        self._closed = True
        self._pending_automatic_reactions.clear()
        await self._read_ops.close()

    async def mark_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int] = None,
        max_id: Optional[int] = None,
    ) -> None:
        await self._read_ops.mark_as_read(chat_id, topic_id, max_id)

    async def mark_automatic_as_read(
        self,
        chat_id: int,
        topic_id: Optional[int],
        max_id: int,
        validate: Callable[[], Awaitable[bool]],
        on_success: Callable[[], Awaitable[None]],
        *,
        selective: bool,
    ) -> None:
        await self._read_ops.mark_automatic_as_read(
            chat_id, topic_id, max_id, validate, on_success, selective=selective
        )

    async def can_selectively_read(
        self, chat_id: int, topic_id: Optional[int], message_id: int
    ) -> bool:
        return await self._read_ops.can_selectively_read(chat_id, topic_id, message_id)

    async def first_unread_message_id(
        self, chat_id: int, topic_id: Optional[int]
    ) -> Optional[int]:
        return await self._read_ops.first_unread_message_id(chat_id, topic_id)

    async def send_reaction(self, chat_id: int, msg_id: int, emoji: str) -> bool:
        async def _do() -> None:
            success = await self._send_reaction(chat_id, msg_id, emoji)
            if not success and self._dispatch_fn:
                try:
                    from src.domain.models import Message
                    from datetime import datetime

                    await self._dispatch_fn(
                        SystemEvent(
                            type="reaction_failed",
                            text="Reaction failed",
                            chat_name="",
                            chat_id=chat_id,
                            message_model=Message(
                                id=msg_id,
                                text="",
                                date=datetime.now(),
                                sender_name="",
                                is_outgoing=False,
                            ),
                        )
                    )
                except Exception:
                    logger.exception(
                        "reaction_failure_dispatch_failed", chat_id=chat_id
                    )

        await self._write_queue.enqueue(_do)
        return True

    async def send_automatic_reaction(
        self,
        chat_id: int,
        msg_id: int,
        emoji: str,
        validate: Callable[[], Awaitable[bool]],
        on_complete: Callable[[bool], Awaitable[None]],
    ) -> bool:
        if self._closed:
            raise RuntimeError("Write operations are closed")
        key = (chat_id, msg_id, emoji)
        if key in self._pending_automatic_reactions:
            return False
        self._pending_automatic_reactions.add(key)

        async def _do() -> None:
            retry = False
            success = False
            try:
                success = await self._send_reaction(
                    chat_id, msg_id, emoji, validate=validate
                )
            except errors.FloodWaitError:
                retry = True
                raise
            finally:
                if not retry:
                    self._pending_automatic_reactions.discard(key)
                    try:
                        await on_complete(success)
                    except Exception:
                        logger.exception(
                            "automatic_reaction_completion_failed", chat_id=chat_id
                        )

        try:
            await self._write_queue.enqueue(_do, automatic=True)
        except BaseException:
            self._pending_automatic_reactions.discard(key)
            raise
        return True

    async def _send_reaction(
        self,
        chat_id: int,
        msg_id: int,
        emoji: str,
        *,
        validate: Optional[Callable[[], Awaitable[bool]]] = None,
    ) -> bool:
        try:
            if validate and (self._closed or not await validate()):
                return False
            entity = await self.client.get_entity(chat_id)
            target = (
                types.ReactionCustomEmoji(document_id=int(emoji))
                if emoji.isdigit()
                else types.ReactionEmoji(emoticon=emoji)
            )
            msgs = await self.client.get_messages(entity, ids=[msg_id])
            if not msgs or not msgs[0]:
                return False
            msg = msgs[0]
            chosen = [
                rc.reaction
                for rc in getattr(getattr(msg, "reactions", None), "results", [])
                if getattr(rc, "chosen_order", None) is not None
            ]
            found = target in chosen
            if validate and found:
                # Automatic reactions express desired state; never toggle it off.
                return True
            new_reactions = [reaction for reaction in chosen if reaction != target]
            if not found:
                new_reactions.append(target)
            if validate and (self._closed or not await validate()):
                return False
            await self.client(
                functions.messages.SendReactionRequest(
                    peer=entity,
                    msg_id=msg_id,
                    reaction=new_reactions,
                    add_to_recent=True,
                )
            )
        except errors.FloodWaitError:
            raise
        except Exception as exc:
            # Never replace other chosen reactions when Telegram rejects this one.
            logger.error(
                "send_reaction_failed",
                chat_id=chat_id,
                msg_id=msg_id,
                category=type(exc).__name__,
            )
            return False

        try:
            updated = await self.client.get_messages(entity, ids=[msg_id])
            if updated and updated[0] and self._dispatch_fn:
                parsed = await self._parser._parse_message(updated[0], chat_id=chat_id)
                await self._dispatch_fn(
                    SystemEvent(
                        type="reaction_update",
                        text="",
                        chat_name="",
                        chat_id=chat_id,
                        message_model=parsed,
                    )
                )
        except Exception as ex:
            logger.error(
                "post_reaction_fetch_failed",
                error=repr(ex),
                traceback=traceback.format_exc(),
            )
        return True
