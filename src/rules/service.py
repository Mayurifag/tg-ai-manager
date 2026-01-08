import re
import asyncio
from typing import Optional
from datetime import datetime
from src.rules.models import Rule, AutoReadRule, RuleType
from src.rules.ports import RuleRepository
from src.domain.models import SystemEvent, ActionLog, Message, ChatType
from src.domain.ports import ActionRepository, ChatRepository
from src.users.ports import UserRepository
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class RuleService:
    def __init__(
        self,
        rule_repo: RuleRepository,
        action_repo: ActionRepository,
        chat_repo: ChatRepository,
        user_repo: UserRepository,
    ):
        self.rule_repo = rule_repo
        self.action_repo = action_repo
        self.chat_repo = chat_repo
        self.user_repo = user_repo

    async def is_autoread_enabled(
        self, chat_id: int, topic_id: Optional[int] = None
    ) -> bool:
        # Check if row exists in DB
        rules = await self.rule_repo.get_by_chat_and_topic(chat_id, topic_id)

        # Priority 1: Check for a specific topic rule
        if topic_id is not None:
            specific_rule = next(
                (r for r in rules if r.topic_id == topic_id and r.rule_type == RuleType.AUTOREAD),
                None,
            )
            if specific_rule:
                return True # If it exists, it is enabled

        # Priority 2: Check for a chat-wide (global) rule
        global_rule = next(
            (r for r in rules if r.topic_id is None and r.rule_type == RuleType.AUTOREAD),
            None,
        )
        if global_rule:
            return True

        return False

    async def check_global_autoread_rules(
        self, message: Message, unread_count: int
    ) -> str:
        if unread_count > 1:
            return ""

        user = await self.user_repo.get_user(1)
        if not user:
            return ""

        # 1. Autoread Service Messages
        if user.autoread_service_messages and message.is_service:
            return "global_service_msg"

        # 2. Autoread Polls
        if user.autoread_polls and message.is_poll:
            return "global_poll"

        # 3. Autoread Self
        if user.autoread_self and message.is_outgoing:
            return "global_self"

        # 4. Autoread Bots
        if user.autoread_bots:
            bots_input = [
                b.strip() for b in user.autoread_bots.split(",") if b.strip()
            ]
            target_usernames = {b.lstrip("@").lower() for b in bots_input}

            sender_username = (message.sender_username or "").lower()
            if sender_username and sender_username in target_usernames:
                return f"global_bot_{sender_username}"

        # 5. Autoread Regex
        if user.autoread_regex and message.text:
            try:
                if re.search(user.autoread_regex, message.text, re.IGNORECASE):
                    return "global_regex"
            except re.error:
                pass

        return ""

    async def handle_new_message_event(self, event: SystemEvent):
        if event.type != "message" or not event.message_model:
            return

        should_read = False
        reason = ""

        if await self.is_autoread_enabled(event.chat_id, event.topic_id):
            should_read = True
            reason = "autoread_rule"

        if not should_read:
            reason = await self.check_global_autoread_rules(
                event.message_model, unread_count=1
            )
            if reason:
                should_read = True

            if should_read and "global" in reason:
                chat = await self.chat_repo.get_chat(event.chat_id)
                if chat and chat.unread_count > 1:
                    should_read = False

        if should_read:
            await self.chat_repo.mark_as_read(event.chat_id, event.topic_id)
            event.is_read = True

            await self.action_repo.add_log(
                ActionLog(
                    action="autoread",
                    chat_id=event.chat_id,
                    chat_name=event.chat_name,
                    reason=reason,
                    date=datetime.now(),
                    link=event.link,
                )
            )

    async def run_startup_scan(self):
        # Prevent crash if adapter not connected yet
        if not hasattr(self.chat_repo, 'is_connected') or not self.chat_repo.is_connected():
            logger.warning("startup_scan_skipped_not_connected")
            return

        logger.info("startup_scan_started")
        try:
            unread_chats = await self.chat_repo.get_all_unread_chats()
            logger.info("startup_scan_found_chats", count=len(unread_chats))

            for chat in unread_chats:
                try:
                    if chat.type == ChatType.FORUM:
                        topics = await self.chat_repo.get_unread_topics(chat.id)
                        for topic in topics:
                            if await self.is_autoread_enabled(chat.id, topic.id):
                                logger.info("startup_autoread_topic", chat_id=chat.id, topic_id=topic.id)
                                await self.chat_repo.mark_as_read(chat.id, topic.id)
                                await self.action_repo.add_log(
                                    ActionLog(
                                        action="startup_read",
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
                                reason = await self.check_global_autoread_rules(msg, chat.unread_count)
                                if reason:
                                    should_read = True

                        if should_read:
                            logger.info("startup_autoread_chat", chat_id=chat.id, reason=reason)
                            await self.chat_repo.mark_as_read(chat.id)
                            await self.action_repo.add_log(
                                ActionLog(
                                    action="startup_read",
                                    chat_id=chat.id,
                                    chat_name=chat.name,
                                    reason=reason,
                                    date=datetime.now(),
                                    link=f"/chat/{chat.id}",
                                )
                            )
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logger.error("startup_scan_chat_error", chat_id=chat.id, error=str(e))
            logger.info("startup_scan_completed")
        except Exception as e:
            logger.error("startup_scan_failed", error=str(e))

    async def toggle_autoread(
        self, chat_id: int, topic_id: Optional[int], enabled: bool
    ) -> Optional[Rule]:
        """
        Adds or Deletes the rule.
        """
        rules = await self.rule_repo.get_by_chat_and_topic(chat_id, topic_id)
        existing = next(
            (r for r in rules if r.rule_type == RuleType.AUTOREAD and r.topic_id == topic_id),
            None,
        )

        if enabled:
            # Create if not exists
            if not existing:
                new_rule = AutoReadRule(
                    user_id=1, # Default user
                    rule_type=RuleType.AUTOREAD,
                    chat_id=chat_id,
                    topic_id=topic_id,
                )
                rule_id = await self.rule_repo.add(new_rule)
                new_rule.id = rule_id
                return new_rule
            return existing
        else:
            # Delete if exists
            if existing and existing.id:
                await self.rule_repo.delete(existing.id)
            return None

    async def apply_autoread_to_all_topics(self, forum_id: int, enabled: bool):
        await self.toggle_autoread(forum_id, None, enabled)
        topics = await self.chat_repo.get_forum_topics(forum_id)
        for topic in topics:
            await self.toggle_autoread(forum_id, topic.id, enabled)
