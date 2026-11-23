import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from telethon import errors, functions, types, utils

from src.adapters.telegram.read_ops import ReadOps
from src.domain.errors import ReactionBlocked
from src.domain.models import Message, SystemEvent
from src.infrastructure.logging import get_logger
from src.infrastructure.tasks import (
    raise_if_cancelled,
    raise_if_task_stopped,
    run_with_task_deadline,
    task_deadline,
)

if TYPE_CHECKING:
    from src.adapters.telegram.message_parser import MessageParser

logger = get_logger(__name__)

# shortcut: allow 10 seconds for pre-write reaction checks; revisit if normal
# Telegram lookups routinely take longer.
PRE_REACTION_TIMEOUT = 10
# shortcut: allow 10 seconds for a best-effort reaction update or failure event;
# revisit if legitimate notifications routinely take longer.
POST_REACTION_UPDATE_TIMEOUT = 10


class WriteOps:
    def __init__(
        self,
        client: Any,
        parser: "MessageParser",
        write_queue: Any,
        dispatch_fn: Optional[Callable[[SystemEvent], Awaitable[None]]],
    ) -> None:
        self.client = client
        self._parser = parser
        self._write_queue = write_queue
        self._dispatch_fn = dispatch_fn  # patched after EventHandlers is built
        self._pending_automatic_reactions: set[tuple[int, int, str]] = set()
        self._uncertain_reactions: set[tuple[int, int]] = set()
        self._reaction_retry_at = 0.0
        self._closed = False
        self._read_ops = ReadOps(
            client=client,
            write_queue=write_queue,
            dispatch_fn=dispatch_fn,
        )

    def set_dispatch_fn(
        self, dispatch_fn: Optional[Callable[[SystemEvent], Awaitable[None]]]
    ) -> None:
        self._dispatch_fn = dispatch_fn
        self._read_ops.set_dispatch_fn(dispatch_fn)

    async def close(self) -> None:
        self._closed = True
        self._pending_automatic_reactions.clear()
        self._uncertain_reactions.clear()
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
        chat_name: str,
        topic_name: Optional[str],
    ) -> bool:
        return await self._read_ops.mark_automatic_as_read(
            chat_id,
            topic_id,
            max_id,
            validate,
            on_success,
            selective=selective,
            chat_name=chat_name,
            topic_name=topic_name,
        )

    async def can_selectively_read(
        self,
        chat_id: int,
        topic_id: Optional[int],
        message_id: int,
        *,
        is_current: Optional[Callable[[], bool]] = None,
    ) -> bool:
        return await self._read_ops.can_selectively_read(
            chat_id, topic_id, message_id, is_current=is_current
        )

    async def send_reaction(self, chat_id: int, msg_id: int, emoji: str) -> bool:
        if self._closed:
            raise RuntimeError("Write operations are closed")
        blocked = self._reaction_blocked(chat_id, msg_id)
        if blocked:
            raise ReactionBlocked(blocked)

        async def _do() -> None:
            if self._closed:
                return
            blocked = self._reaction_blocked(chat_id, msg_id)
            if blocked:
                await self._notify_reaction(
                    chat_id, msg_id, "reaction_blocked", blocked
                )
                return
            result = await self._send_reaction(chat_id, msg_id, emoji)
            if result is None:
                await self._notify_reaction(
                    chat_id,
                    msg_id,
                    "reaction_uncertain",
                    "Reaction outcome unknown; it may still apply.",
                )
            elif not result:
                await self._notify_reaction(
                    chat_id, msg_id, "reaction_failed", "Reaction failed"
                )

        await self._write_queue.enqueue(_do)
        return True

    async def send_automatic_reaction(
        self,
        chat_id: int,
        msg_id: int,
        emoji: str,
        validate: Callable[[], Awaitable[bool]],
        on_complete: Callable[[Optional[bool]], Awaitable[None]],
    ) -> bool:
        if self._closed:
            raise RuntimeError("Write operations are closed")
        if self._reaction_blocked(chat_id, msg_id):
            return False
        key = (chat_id, msg_id, emoji)
        if key in self._pending_automatic_reactions:
            return False
        self._pending_automatic_reactions.add(key)

        async def _do() -> None:
            cancelled = False
            success = None
            try:
                if self._reaction_blocked(chat_id, msg_id):
                    success = False
                else:
                    success = await self._send_reaction(
                        chat_id, msg_id, emoji, validate=validate
                    )
            except asyncio.CancelledError:
                cancelled = True
                raise
            finally:
                self._pending_automatic_reactions.discard(key)
                try:
                    deadline = (
                        asyncio.get_running_loop().time() + POST_REACTION_UPDATE_TIMEOUT
                    )
                    await run_with_task_deadline(
                        lambda: on_complete(success),
                        deadline,
                        name="automatic_reaction_completion",
                        allow_pending_cancellation=cancelled,
                    )
                except Exception as error:
                    logger.error(
                        "automatic_reaction_completion_failed",
                        chat_id=chat_id,
                        category=type(error).__name__,
                    )

        try:
            await self._write_queue.enqueue(_do, automatic=True)
        except BaseException:
            self._pending_automatic_reactions.discard(key)
            raise
        return True

    def _reaction_blocked(self, chat_id: int, msg_id: int) -> Optional[str]:
        if (chat_id, msg_id) in self._uncertain_reactions:
            return "A previous reaction may still apply; this reaction was not sent."
        remaining = self._reaction_retry_at - asyncio.get_running_loop().time()
        if remaining > 0:
            return (
                f"Telegram is limiting reactions. Try again in {int(remaining) + 1}s."
            )
        return None

    async def _notify_reaction(
        self, chat_id: int, msg_id: int, event_type: str, text: str
    ) -> None:
        dispatch_fn = self._dispatch_fn
        if not dispatch_fn:
            return
        event = SystemEvent(
            type=event_type,
            text=text,
            chat_name="",
            chat_id=chat_id,
            message_model=Message(
                id=msg_id,
                text="",
                date=datetime.now(timezone.utc),
                sender_name="",
                is_outgoing=False,
            ),
        )
        deadline = asyncio.get_running_loop().time() + POST_REACTION_UPDATE_TIMEOUT
        try:
            await run_with_task_deadline(
                lambda: dispatch_fn(event), deadline, name="reaction_notification"
            )
        except Exception as error:
            logger.error(
                "reaction_notification_failed",
                chat_id=chat_id,
                category=type(error).__name__,
            )

    def _record_uncertain_reaction(self, chat_id: int, msg_id: int) -> None:
        if not self._closed:
            self._uncertain_reactions.add((chat_id, msg_id))
        logger.warning("reaction_outcome_unknown", chat_id=chat_id, msg_id=msg_id)

    async def _send_reaction(
        self,
        chat_id: int,
        msg_id: int,
        emoji: str,
        *,
        validate: Optional[Callable[[], Awaitable[bool]]] = None,
    ) -> Optional[bool]:
        raise_if_cancelled()
        loop = asyncio.get_running_loop()
        owner = asyncio.current_task()
        preflight_deadline = loop.time() + PRE_REACTION_TIMEOUT
        rpc_started = False
        uncertainty_recorded = False

        def record_uncertain() -> None:
            nonlocal uncertainty_recorded
            if not uncertainty_recorded:
                uncertainty_recorded = True
                self._record_uncertain_reaction(chat_id, msg_id)

        async def perform() -> tuple[Optional[bool], Any, Any]:
            nonlocal rpc_started
            already_chosen = False
            try:
                with task_deadline(preflight_deadline, cancel_with=owner):
                    async with asyncio.timeout_at(preflight_deadline) as timeout:
                        raise_if_task_stopped()
                        if self._closed:
                            return False, None, None
                        if validate:
                            valid = await validate()
                            raise_if_task_stopped()
                            if not valid:
                                return False, None, None
                        raise_if_task_stopped()
                        entity = await self.client.get_input_entity(chat_id)
                        raise_if_task_stopped()
                        target = (
                            types.ReactionCustomEmoji(document_id=int(emoji))
                            if emoji.isdigit()
                            else types.ReactionEmoji(emoticon=emoji)
                        )
                        raise_if_task_stopped()
                        msgs = await self.client.get_messages(entity, ids=[msg_id])
                        raise_if_task_stopped()
                        msg = msgs[0] if msgs else None
                        if not msg:
                            raise_if_task_stopped()
                            return False, None, None
                        chosen = [
                            rc.reaction
                            for rc in getattr(
                                getattr(msg, "reactions", None), "results", []
                            )
                            if getattr(rc, "chosen_order", None) is not None
                        ]
                        found = target in chosen
                        raise_if_task_stopped()
                        if validate and found:
                            # Automatic reactions express desired state.
                            already_chosen = True
                        else:
                            new_reactions = [
                                reaction for reaction in chosen if reaction != target
                            ]
                            if not found:
                                new_reactions.append(target)
                            raise_if_task_stopped()
                            if self._closed:
                                return False, None, None
                            if validate:
                                valid = await validate()
                                raise_if_task_stopped()
                                if not valid:
                                    return False, None, None
                            raise_if_task_stopped()
                if timeout.expired() or loop.time() >= preflight_deadline:
                    logger.warning(
                        "reaction_preflight_timed_out", chat_id=chat_id, msg_id=msg_id
                    )
                    return False, None, None
            except Exception as exc:
                if owner is not None and owner.cancelling():
                    raise asyncio.CancelledError
                raise_if_cancelled()
                logger.error(
                    "send_reaction_failed",
                    chat_id=chat_id,
                    msg_id=msg_id,
                    category=type(exc).__name__,
                )
                return False, None, None

            if self._closed:
                return False, None, None
            if already_chosen:
                return True, None, None

            try:
                with task_deadline(float("inf"), cancel_with=owner):
                    raise_if_task_stopped()
                    rpc_started = True
                    result = await self.client.send_once(
                        functions.messages.SendReactionRequest(
                            peer=entity,
                            msg_id=msg_id,
                            reaction=new_reactions,
                            add_to_recent=True,
                        )
                    )
                    raise_if_task_stopped()
                    if not isinstance(result, types.TypeUpdates):
                        record_uncertain()
                        return None, None, None
            except (errors.FloodWaitError, errors.FloodPremiumWaitError) as exc:
                self._reaction_retry_at = loop.time() + max(1, exc.seconds)
                return False, None, None
            except errors.MessageNotModifiedError:
                return True, None, None
            except errors.RPCError as exc:
                if abs(exc.code or 0) < 500 and not isinstance(
                    exc, errors.TimedOutError
                ):
                    logger.warning(
                        "reaction_rejected",
                        chat_id=chat_id,
                        msg_id=msg_id,
                        category=type(exc).__name__,
                    )
                    return False, None, None
                record_uncertain()
                return None, None, None
            except asyncio.CancelledError:
                if rpc_started:
                    record_uncertain()
                raise
            except Exception:
                if owner is not None and owner.cancelling():
                    if rpc_started:
                        record_uncertain()
                    raise asyncio.CancelledError
                raise_if_cancelled()
                if rpc_started:
                    record_uncertain()
                    return None, None, None
                return False, None, None
            return True, entity, result

        # Keep timeout cancellation in one owned child without a scheduling
        # gap between final rule validation and the mutation.
        operation = asyncio.create_task(perform(), name="reaction_attempt")
        try:
            success, entity, result = await asyncio.shield(operation)
            raise_if_cancelled()
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError) and rpc_started:
                record_uncertain()
            if not operation.done():
                operation.cancel()
                # Join even if the worker is cancelled again during cleanup.
                while not operation.done():
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
            if operation.done() and not operation.cancelled():
                operation.exception()
            raise

        if result is None or self._dispatch_fn is None:
            return success

        async def refresh() -> None:
            raise_if_task_stopped()
            updates = (
                (result.update,)
                if isinstance(result, types.UpdateShort)
                else getattr(result, "updates", ())
            )
            update = next(
                (
                    item
                    for item in reversed(updates)
                    if isinstance(item, types.UpdateMessageReactions)
                    and item.msg_id == msg_id
                    and utils.get_peer_id(item.peer) == chat_id
                    and item.saved_peer_id is None
                ),
                None,
            )
            if update is not None and not update.reactions.min:
                reactions = update.reactions
            else:
                updated = await self.client.get_messages(entity, ids=[msg_id])
                raise_if_task_stopped()
                if not updated or not updated[0]:
                    return
                reactions = getattr(updated[0], "reactions", None)
            if dispatch_fn := self._dispatch_fn:
                await dispatch_fn(
                    SystemEvent(
                        type="reaction_update",
                        text="",
                        chat_name="",
                        chat_id=chat_id,
                        message_model=Message(
                            id=msg_id,
                            text="",
                            date=datetime.now(timezone.utc),
                            sender_name="",
                            is_outgoing=False,
                            reactions=self._parser._extract_reactions(reactions),
                        ),
                    )
                )

        try:
            deadline = loop.time() + POST_REACTION_UPDATE_TIMEOUT
            await run_with_task_deadline(refresh, deadline, name="reaction_refresh")
        except Exception as ex:
            logger.error(
                "post_reaction_fetch_failed",
                chat_id=chat_id,
                msg_id=msg_id,
                category=type(ex).__name__,
            )
        return True
