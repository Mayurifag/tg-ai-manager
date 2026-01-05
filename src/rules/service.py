import re
import asyncio
from typing import List, Optional
from datetime import datetime
from src.rules.models import Rule, AutoReadRule, RuleType
from src.rules.ports import RuleRepository
from src.domain.models import SystemEvent, ActionLog, Message, ChatType
from src.domain.ports import ActionRepository, ChatRepository
from src.settings.ports import SettingsRepository
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

class RuleService:
    def __init__(self,
                 rule_repo: RuleRepository,
                 action_repo: ActionRepository,
                 chat_repo: ChatRepository,
                 settings_repo: SettingsRepository):
        self.rule_repo = rule_repo
        self.action_repo = action_repo
        self.chat_repo = chat_repo
        self.settings_repo = settings_repo

    async def is_autoread_enabled(self, chat_id: int, topic_id: Optional[int] = None) -> bool:
        rules = await self.rule_repo.get_by_chat_and_topic(chat_id, topic_id)

        # Priority 1: Check for a specific topic rule if we are in a topic
        if topic_id is not None:
            specific_rule = next(
                (r for r in rules if r.topic_id == topic_id and r.rule_type == RuleType.AUTOREAD),
                None
            )
            if specific_rule:
                return specific_rule.enabled

        # Priority 2: Check for a chat-wide (global) rule
        global_rule = next(
            (r for r in rules if r.topic_id is None and r.rule_type == RuleType.AUTOREAD),
            None
        )
        if global_rule:
            return global_rule.enabled

        return False

    async def check_global_autoread_rules(self, message: Message, unread_count: int) -> str:
        """
        Checks global settings and returns the reason if it should be autoread.
        Returns empty string if no rule matches.

        Condition: Only proceeds if unread_count is 1 (no backlog).
        """
        if unread_count > 1:
            return ""

        settings = await self.settings_repo.get_settings()

        # 1. Autoread Service Messages
        if settings.autoread_service_messages and message.is_service:
            return "global_service_msg"

        # 2. Autoread Polls
        if settings.autoread_polls and message.is_poll:
            return "global_poll"

        # 3. Autoread Self
        if settings.autoread_self and message.is_outgoing:
            return "global_self"

        # 4. Autoread Bots
        if settings.autoread_bots:
            bots_input = [b.strip() for b in settings.autoread_bots.split(',') if b.strip()]
            target_usernames = {b.lstrip('@').lower() for b in bots_input}

            sender_username = (message.sender_username or "").lower()
            if sender_username and sender_username in target_usernames:
                return f"global_bot_{sender_username}"

        # 5. Autoread Regex
        if settings.autoread_regex and message.text:
            try:
                if re.search(settings.autoread_regex, message.text, re.IGNORECASE):
                    return "global_regex"
            except re.error:
                pass

        return ""

    async def handle_new_message_event(self, event: SystemEvent):
        if event.type != "message" or not event.message_model:
            return

        should_read = False
        reason = ""

        # 1. Check Chat Specific Rules (Always take priority, ignoring unread count)
        if await self.is_autoread_enabled(event.chat_id, event.topic_id):
            should_read = True
            reason = "autoread_rule"

        # 2. Check Global Rules (Only if chat specific rule didn't trigger)
        # Assuming unread_count is effectively 1 since it's a real-time new message
        if not should_read:
            # We treat the event as if unread_count=1 for the check logic
            reason = await self.check_global_autoread_rules(event.message_model, unread_count=1)
            if reason:
                should_read = True

            # Additional safety: Double check real unread count?
            # The prompt implies we trust the event stream logic for "new events",
            # but strict backlog checking happens in check_global_autoread_rules via explicit unread_count argument usually.
            # Here we pass 1 because it's a new event.
            # However, if the chat actually has backlog, we might want to skip.
            # We can check chat details, but that's an extra await.
            # For now, we stick to the logic that 'check_global' is for smart filtering of single messages.
            # But wait, previously we had `if chat.unread_count > 1: return ""`.
            # We should probably keep that safety.
            if should_read and "global" in reason:
                 chat = await self.chat_repo.get_chat(event.chat_id)
                 if chat and chat.unread_count > 1:
                     should_read = False

        if should_read:
            await self.chat_repo.mark_as_read(event.chat_id, event.topic_id)
            event.is_read = True

            await self.action_repo.add_log(ActionLog(
                action="autoread",
                chat_id=event.chat_id,
                chat_name=event.chat_name,
                reason=reason,
                date=datetime.now(),
                link=event.link
            ))

    async def run_startup_scan(self):
        """
        Background task to scan all unread chats on startup and apply rules.
        """
        logger.info("startup_scan_started")
        try:
            unread_chats = await self.chat_repo.get_all_unread_chats()
            logger.info("startup_scan_found_chats", count=len(unread_chats))

            for chat in unread_chats:
                try:
                    # Case 1: Forum
                    if chat.type == ChatType.FORUM:
                        topics = await self.chat_repo.get_unread_topics(chat.id)
                        for topic in topics:
                            # Apply Explicit Rules (or inherited Forum rules)
                            # Explicit rules override everything (Backlog is ignored)
                            if await self.is_autoread_enabled(chat.id, topic.id):
                                logger.info("startup_autoread_topic", chat_id=chat.id, topic_id=topic.id)
                                await self.chat_repo.mark_as_read(chat.id, topic.id)
                                await self.action_repo.add_log(ActionLog(
                                    action="startup_read",
                                    chat_id=chat.id,
                                    chat_name=f"{chat.name} (Topic {topic.id})",
                                    reason="autoread_rule_startup",
                                    date=datetime.now(),
                                    link=f"/forum/{chat.id}"
                                ))

                    # Case 2: Standard Chat
                    else:
                        should_read = False
                        reason = ""

                        # 2a. Explicit Rule (Ignores backlog count)
                        if await self.is_autoread_enabled(chat.id):
                            should_read = True
                            reason = "autoread_rule_startup"

                        # 2b. Global Rules (Only if NO explicit rule and NO backlog)
                        elif chat.unread_count == 1:
                            # Fetch the single unread message to check against rules
                            msgs = await self.chat_repo.get_messages(chat.id, limit=1)
                            if msgs:
                                msg = msgs[0]
                                reason = await self.check_global_autoread_rules(msg, chat.unread_count)
                                if reason:
                                    should_read = True

                        if should_read:
                            logger.info("startup_autoread_chat", chat_id=chat.id, reason=reason)
                            await self.chat_repo.mark_as_read(chat.id)
                            await self.action_repo.add_log(ActionLog(
                                action="startup_read",
                                chat_id=chat.id,
                                chat_name=chat.name,
                                reason=reason,
                                date=datetime.now(),
                                link=f"/chat/{chat.id}"
                            ))

                    # Slight delay to prevent flooding
                    await asyncio.sleep(0.1)

                except Exception as e:
                    logger.error("startup_scan_chat_error", chat_id=chat.id, error=str(e))

            logger.info("startup_scan_completed")

        except Exception as e:
            logger.error("startup_scan_failed", error=str(e))

    async def toggle_autoread(self, chat_id: int, topic_id: Optional[int], enabled: bool) -> Rule:
        rules = await self.rule_repo.get_by_chat_and_topic(chat_id, topic_id)

        autoread_rule = next(
            (r for r in rules if r.rule_type == RuleType.AUTOREAD and r.topic_id == topic_id),
            None
        )

        if autoread_rule:
            autoread_rule.enabled = enabled
            autoread_rule.updated_at = datetime.now()
            await self.rule_repo.update(autoread_rule)
            return autoread_rule
        else:
            new_rule = AutoReadRule(
                rule_type=RuleType.AUTOREAD,
                chat_id=chat_id,
                topic_id=topic_id,
                enabled=enabled
            )
            rule_id = await self.rule_repo.add(new_rule)
            new_rule.id = rule_id
            return new_rule

    async def apply_autoread_to_all_topics(self, forum_id: int, enabled: bool):
        await self.toggle_autoread(forum_id, None, enabled)
        topics = await self.chat_repo.get_forum_topics(forum_id)
        for topic in topics:
            await self.toggle_autoread(forum_id, topic.id, enabled)
