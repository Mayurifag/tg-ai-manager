import asyncio
import re
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from src.domain.models import ActionLog, ChatType, Message, SystemEvent
from src.domain.ports import ActionRepository, ChatRepository
from src.infrastructure.logging import get_logger
from src.infrastructure.queue_service import QueueService
from src.rules.models import Rule, RuleType
from src.rules.ports import RuleRepository
from src.users.ports import UserRepository

logger = get_logger(__name__)


class RuleService:
    def __init__(
        self,
        rule_repo: RuleRepository,
        action_repo: ActionRepository,
        chat_repo: ChatRepository,
        user_repo: UserRepository,
        queue_service: QueueService,
    ):
        self.rule_repo = rule_repo
        self.action_repo = action_repo
        self.chat_repo = chat_repo
        self.user_repo = user_repo
        self.queue_service = queue_service

        # Cache for deduplicating album reactions: (chat_id, grouped_id) -> timestamp
        self._album_reaction_cache: Dict[Tuple[int, int], float] = {}

    async def get_rule(
        self, chat_id: int, topic_id: Optional[int], rule_type: RuleType
    ) -> Optional[Rule]:
        rules = await self.rule_repo.get_by_chat_and_topic(chat_id, topic_id)
        # Priority: Specific Topic > Global
        if topic_id is not None:
            specific = next(
                (
                    r
                    for r in rules
                    if r.topic_id == topic_id and r.rule_type == rule_type
                ),
                None,
            )
            if specific:
                return specific

        global_rule = next(
            (r for r in rules if r.topic_id is None and r.rule_type == rule_type), None
        )
        return global_rule

    async def is_autoread_enabled(
        self, chat_id: int, topic_id: Optional[int] = None
    ) -> bool:
        rule = await self.get_rule(chat_id, topic_id, RuleType.AUTOREAD)
        return bool(rule)

    async def check_global_autoread_rules(
        self, message: Message, unread_count: int
    ) -> str:
        if unread_count > 1:
            return ""

        user = await self.user_repo.get_user(1)
        if not user:
            return ""

        if user.autoread_service_messages and message.is_service:
            return "global_service_msg"

        if user.autoread_polls and message.is_poll:
            return "global_poll"

        if user.autoread_self and message.is_outgoing:
            return "global_self"

        if user.autoread_bots:
            bots_input = [b.strip() for b in user.autoread_bots.split(",") if b.strip()]
            target_usernames = {b.lstrip("@").lower() for b in bots_input}

            sender_username = (message.sender_username or "").lower()
            if sender_username and sender_username in target_usernames:
                return f"global_bot_{sender_username}"

        if user.autoread_regex and message.text:
            try:
                if re.search(user.autoread_regex, message.text, re.IGNORECASE):
                    return "global_regex"
            except re.error:
                pass

        return ""

    async def handle_new_message_event(self, event: SystemEvent):
        # Strict type guard: we can only process events with a valid chat_id
        if not event.chat_id:
            return

        if event.type != "message" or not event.message_model:
            return

        msg = event.message_model

        # --- AutoRead Logic ---
        should_read = False
        reason = ""

        if await self.is_autoread_enabled(event.chat_id, event.topic_id):
            should_read = True
            reason = "autoread_rule"

        if not should_read:
            # We assume unread_count is 1 for a new message event for simplicity in global rules
            # In reality, interactor fetches real state, but here we estimate.
            reason = await self.check_global_autoread_rules(msg, unread_count=1)
            if reason:
                should_read = True

            if should_read and "global" in reason:
                # Double check with real state
                chat = await self.chat_repo.get_chat(event.chat_id)
                if chat and chat.unread_count > 1:
                    should_read = False

        if should_read:
            max_id = msg.id
            await self.queue_service.enqueue_mark_read(
                event.chat_id, event.topic_id, max_id=max_id
            )
            event.is_read = True

            await self.action_repo.add_log(
                ActionLog(
                    action="autoread_queued",
                    chat_id=event.chat_id,
                    chat_name=event.chat_name,
                    reason=reason,
                    date=datetime.now(),
                    link=event.link,
                )
            )

        # --- AutoReact Logic ---
        await self.apply_autoreact(event.chat_id, event.topic_id, msg)

    async def apply_autoreact(
        self, chat_id: int, topic_id: Optional[int], message: Message
    ):
        if message.is_outgoing:
            return

        # Album Deduplication Logic
        if message.grouped_id:
            now = time.time()
            # Clean up old cache entries (older than 60s)
            self._album_reaction_cache = {
                k: v for k, v in self._album_reaction_cache.items() if now - v < 60
            }

            key = (chat_id, message.grouped_id)
            if key in self._album_reaction_cache:
                # Already reacted to this album group recently
                return

            # Mark this album as processed
            self._album_reaction_cache[key] = now

        rule = await self.get_rule(chat_id, topic_id, RuleType.AUTOREACT)
        if not rule:
            return

        emoji = rule.config.get("emoji", "💩")
        target_users = rule.config.get("target_users", [])

        should_react = False

        if not target_users:
            # Empty list = React to EVERYTHING (except self, checked above)
            should_react = True
        else:
            if message.sender_id in target_users:
                should_react = True

        if should_react:
            # Check if already reacted by me
            already_reacted = False
            for r in message.reactions:
                if r.is_chosen:
                    if r.emoji == emoji or (
                        r.custom_emoji_id and str(r.custom_emoji_id) == emoji
                    ):
                        already_reacted = True
                        break

            if not already_reacted:
                await self.queue_service.enqueue_reaction(chat_id, message.id, emoji)
                # We don't log actions for reactions to avoid spamming the log

    async def run_startup_scan(self):
        if (
            not hasattr(self.chat_repo, "is_connected")
            or not self.chat_repo.is_connected()
        ):
            return

        try:
            unread_chats = await self.chat_repo.get_all_unread_chats()
            for chat in unread_chats:
                try:
                    if chat.type == ChatType.FORUM:
                        topics = await self.chat_repo.get_unread_topics(chat.id)
                        for topic in topics:
                            if await self.is_autoread_enabled(chat.id, topic.id):
                                await self.queue_service.enqueue_mark_read(
                                    chat.id, topic.id
                                )
                                await self.action_repo.add_log(
                                    ActionLog(
                                        action="startup_read_queued",
                                        chat_id=chat.id,
                                        chat_name=f"{chat.name} (Topic {topic.id})",
                                        reason="autoread_rule_startup",
                                        date=datetime.now(),
                                        link=f"/forum/{chat.id}",
                                    )
                                )
                    else:
                        should_read = False
                        reason = ""
                        if await self.is_autoread_enabled(chat.id):
                            should_read = True
                            reason = "autoread_rule_startup"
                        elif chat.unread_count == 1:
                            msgs = await self.chat_repo.get_messages(chat.id, limit=1)
                            if msgs:
                                msg = msgs[0]
                                reason = await self.check_global_autoread_rules(
                                    msg, chat.unread_count
                                )
                                if reason:
                                    should_read = True
                        if should_read:
                            await self.queue_service.enqueue_mark_read(chat.id)
                            await self.action_repo.add_log(
                                ActionLog(
                                    action="startup_read_queued",
                                    chat_id=chat.id,
                                    chat_name=chat.name,
                                    reason=reason,
                                    date=datetime.now(),
                                    link=f"/chat/{chat.id}",
                                )
                            )
                    await asyncio.sleep(0.1)
                except Exception:
                    pass
        except Exception:
            pass

    async def toggle_autoread(
        self, chat_id: int, topic_id: Optional[int], enabled: bool
    ) -> Optional[Rule]:
        return await self._toggle_rule(chat_id, topic_id, RuleType.AUTOREAD, enabled)

    async def set_autoreact(
        self,
        chat_id: int,
        topic_id: Optional[int],
        enabled: bool,
        config: Dict[str, Any],
    ) -> Optional[Rule]:
        return await self._toggle_rule(
            chat_id, topic_id, RuleType.AUTOREACT, enabled, config
        )

    async def _toggle_rule(
        self,
        chat_id: int,
        topic_id: Optional[int],
        rule_type: RuleType,
        enabled: bool,
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[Rule]:
        rules = await self.rule_repo.get_by_chat_and_topic(chat_id, topic_id)
        existing = next(
            (r for r in rules if r.rule_type == rule_type and r.topic_id == topic_id),
            None,
        )

        if enabled:
            if not existing:
                new_rule = Rule(
                    user_id=1,
                    rule_type=rule_type,
                    chat_id=chat_id,
                    topic_id=topic_id,
                    config=config or {},
                )
                rule_id = await self.rule_repo.add(new_rule)
                new_rule.id = rule_id
                return new_rule
            else:
                if config is not None:
                    existing.config = config
                    await self.rule_repo.update(existing)
                return existing
        else:
            if existing and existing.id:
                await self.rule_repo.delete(existing.id)
            return None

    async def apply_autoread_to_all_topics(self, forum_id: int, enabled: bool):
        await self.toggle_autoread(forum_id, None, enabled)
        topics = await self.chat_repo.get_forum_topics(forum_id)
        for topic in topics:
            await self.toggle_autoread(forum_id, topic.id, enabled)

    async def simulate_process_message(
        self, chat_id: int, msg_id: int
    ) -> Dict[str, Any]:
        """Dry run debug for a specific message."""
        # We rely on the repo supporting 'ids' now
        msgs = await self.chat_repo.get_messages(chat_id, ids=[msg_id])
        msg = msgs[0] if msgs else None

        if not msg:
            return {"error": "Message not found"}

        results = {}

        # 1. Autoread Check
        ar_rule = await self.is_autoread_enabled(
            chat_id, None
        )  # Ignoring topic for now or need topic_id

        ar_status = "skipped"
        ar_reason = "disabled"

        if ar_rule:
            ar_status = "would_read"
            ar_reason = "rule_enabled"
        else:
            global_reason = await self.check_global_autoread_rules(msg, unread_count=1)
            if global_reason:
                ar_status = "would_read"
                ar_reason = global_reason

        results["autoread"] = {"status": ar_status, "reason": ar_reason}

        # 2. Autoreact Check
        react_rule = await self.get_rule(chat_id, None, RuleType.AUTOREACT)
        react_status = "skipped"
        react_detail = "no_rule"

        if react_rule:
            emoji = react_rule.config.get("emoji", "💩")
            target_users = react_rule.config.get("target_users", [])
            should_react = False

            if not target_users:
                should_react = True
            else:
                if msg.sender_id in target_users:
                    should_react = True

            if should_react:
                react_status = "would_react"
                react_detail = f"emoji: {emoji}"
            else:
                react_status = "skipped"
                react_detail = "sender_mismatch"

        results["autoreact"] = {"status": react_status, "detail": react_detail}

        return results
