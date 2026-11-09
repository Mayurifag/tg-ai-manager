import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from telethon import errors, functions, types

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
from src.infrastructure.telegram_queue import UncertainFloodWait

if TYPE_CHECKING:
    from src.adapters.telegram.message_parser import MessageParser

logger = get_logger(__name__)

# shortcut: allow 10 seconds for pre-write reaction checks; revisit if normal
# Telegram lookups routinely take longer.
PRE_REACTION_TIMEOUT = 10
# shortcut: allow 90 seconds for a reaction RPC, including Telethon's internal
# flood waits; revisit if normal Telegram requests routinely take longer.
REACTION_WRITE_TIMEOUT = 90
# shortcut: stop further reactions after 32 unknown outcomes; revisit when late
# mutations can be ruled out or reconciled safely.
MAX_UNCERTAIN_REACTIONS = 32
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
        get_topic_name_fn: Callable[[int, int], Awaitable[Optional[str]]],
        *,
        get_topic_name_before_fn: Optional[
            Callable[[int, int, float], Awaitable[Optional[str]]]
        ] = None,
    ) -> None:
        self.client = client
        self._parser = parser
        self._write_queue = write_queue
        self._dispatch_fn = dispatch_fn  # patched after EventHandlers is built
        self._pending_automatic_reactions: set[tuple[int, int, str]] = set()
        self._uncertain_reactions: set[tuple[int, int]] = set()
        self._closed = False
        self._read_ops = ReadOps(
            client=client,
            write_queue=write_queue,
            dispatch_fn=dispatch_fn,
            get_topic_name_fn=get_topic_name_fn,
            get_topic_name_before_fn=get_topic_name_before_fn,
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
    ) -> None:
        await self._read_ops.mark_automatic_as_read(
            chat_id, topic_id, max_id, validate, on_success, selective=selective
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
            try:
                result = await self._send_reaction(chat_id, msg_id, emoji)
            except UncertainFloodWait:
                await self._notify_reaction(
                    chat_id,
                    msg_id,
                    "reaction_uncertain",
                    "Reaction outcome unknown; it may still apply.",
                )
                raise
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
            retry = False
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
            except errors.FloodWaitError:
                retry = True
                raise
            finally:
                if not retry:
                    self._pending_automatic_reactions.discard(key)
                    try:
                        deadline = (
                            asyncio.get_running_loop().time()
                            + POST_REACTION_UPDATE_TIMEOUT
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
        if len(self._uncertain_reactions) >= MAX_UNCERTAIN_REACTIONS:
            return (
                "Too many reactions have unknown outcomes; this reaction was not sent."
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

        async def perform() -> tuple[Optional[bool], Any, bool]:
            nonlocal rpc_started
            already_chosen = False
            try:
                with task_deadline(preflight_deadline, cancel_with=owner):
                    async with asyncio.timeout_at(preflight_deadline) as timeout:
                        raise_if_task_stopped()
                        if self._closed:
                            return False, None, False
                        if validate:
                            valid = await validate()
                            raise_if_task_stopped()
                            if not valid:
                                return False, None, False
                        raise_if_task_stopped()
                        entity = await self.client.get_entity(chat_id)
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
                            return False, None, False
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
                                return False, None, False
                            if validate:
                                valid = await validate()
                                raise_if_task_stopped()
                                if not valid:
                                    return False, None, False
                            raise_if_task_stopped()
                if timeout.expired() or loop.time() >= preflight_deadline:
                    logger.warning(
                        "reaction_preflight_timed_out", chat_id=chat_id, msg_id=msg_id
                    )
                    return False, None, False
            except errors.FloodWaitError:
                raise
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
                return False, None, False

            if self._closed:
                return False, None, False
            if already_chosen:
                return True, None, False

            write_deadline = loop.time() + REACTION_WRITE_TIMEOUT
            try:
                with task_deadline(write_deadline, cancel_with=owner):
                    async with asyncio.timeout_at(write_deadline) as timeout:
                        raise_if_task_stopped()
                        rpc_started = True
                        result = await self.client(
                            functions.messages.SendReactionRequest(
                                peer=entity,
                                msg_id=msg_id,
                                reaction=new_reactions,
                                add_to_recent=True,
                            )
                        )
                        raise_if_task_stopped()
                    raise_if_task_stopped()
                    if timeout.expired() or not isinstance(result, types.TypeUpdates):
                        record_uncertain()
                        return None, None, False
            except errors.FloodWaitError as exc:
                # Telethon may have retried before exposing this wait.
                record_uncertain()
                raise UncertainFloodWait(exc.seconds) from None
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
                    return None, None, False
                return False, None, False
            return True, entity, True

        # Keep timeout cancellation in one owned child without a scheduling
        # gap between final rule validation and the mutation.
        operation = asyncio.create_task(perform(), name="reaction_attempt")
        try:
            success, entity, refresh_needed = await asyncio.shield(operation)
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

        if not refresh_needed:
            return success

        async def refresh() -> None:
            avatar_generation = self._parser._media.avatar_generation()
            raise_if_task_stopped()
            updated = await self.client.get_messages(entity, ids=[msg_id])
            raise_if_task_stopped()
            message = updated[0] if updated else None
            if message and self._dispatch_fn:
                raise_if_task_stopped()
                parsed = await self._parser._parse_message(
                    message,
                    chat_id=chat_id,
                    avatar_generation=avatar_generation,
                )
                raise_if_task_stopped()
                dispatch_fn = self._dispatch_fn
                if dispatch_fn:
                    raise_if_task_stopped()
                    await dispatch_fn(
                        SystemEvent(
                            type="reaction_update",
                            text="",
                            chat_name="",
                            chat_id=chat_id,
                            message_model=parsed,
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
