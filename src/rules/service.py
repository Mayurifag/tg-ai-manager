import re
from typing import List, Optional
from datetime import datetime
from src.rules.models import Rule, AutoReadRule, RuleType
from src.rules.ports import RuleRepository
from src.domain.models import SystemEvent, ActionLog
from src.domain.ports import ActionRepository, ChatRepository
from src.settings.ports import SettingsRepository

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

    async def _should_smart_autoread(self, event: SystemEvent) -> str:
        """
        Checks global settings and returns the reason if it should be autoread,
        otherwise returns empty string.

        Condition: Only proceeds if this is the ONLY unread message in the chat/topic.
        """
        # 0. Check Hard Condition: Must be the only new message
        # We assume the event just happened, so unread_count should be 1.
        # If it's > 1, it means there's a backlog, so we skip global logic.
        chat = await self.chat_repo.get_chat(event.chat_id)
        if not chat:
            return ""

        # Note: Depending on timing, unread_count might be 0 (if sync is fast) or 1.
        # If it's 2+, we definitely skip.
        if chat.unread_count > 1:
            return ""

        settings = await self.settings_repo.get_settings()
        msg = event.message_model
        if not msg:
            return ""

        # 1. Autoread Service Messages (Pins, Joins, Leaves, Photo Changes, etc.)
        if settings.autoread_service_messages and msg.is_service:
            return "global_service_msg"

        # 2. Autoread Polls
        if settings.autoread_polls and msg.is_poll:
            return "global_poll"

        # 3. Autoread Self
        if settings.autoread_self and msg.is_outgoing:
            return "global_self"

        # 4. Autoread Bots (By Username only)
        if settings.autoread_bots:
            bots_input = [b.strip() for b in settings.autoread_bots.split(',') if b.strip()]
            # Normalize: remove leading @ and lower case
            target_usernames = {b.lstrip('@').lower() for b in bots_input}

            sender_username = (msg.sender_username or "").lower()
            if sender_username and sender_username in target_usernames:
                return f"global_bot_{sender_username}"

        # 5. Autoread Regex
        if settings.autoread_regex and msg.text:
            try:
                if re.search(settings.autoread_regex, msg.text, re.IGNORECASE):
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

        # 2. Check Global Smart Rules (Only if chat specific rule didn't trigger)
        if not should_read:
            reason = await self._should_smart_autoread(event)
            if reason:
                should_read = True

        if should_read:
            # Perform actual read operation
            await self.chat_repo.mark_as_read(event.chat_id, event.topic_id)

            # Set flag on event so frontend knows not to increment unread counter
            event.is_read = True

            chat_name = event.chat_name

            log = ActionLog(
                action="autoread",
                chat_id=event.chat_id,
                chat_name=chat_name,
                reason=reason,
                date=datetime.now(),
                link=event.link
            )
            await self.action_repo.add_log(log)

    async def toggle_autoread(self, chat_id: int, topic_id: Optional[int], enabled: bool) -> Rule:
        rules = await self.rule_repo.get_by_chat_and_topic(chat_id, topic_id)

        # Find exact match for the requested scope (specific topic or global)
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
        # 1. Update/Create global forum rule
        await self.toggle_autoread(forum_id, None, enabled)

        # 2. Update/Create rule for every existing topic
        topics = await self.chat_repo.get_forum_topics(forum_id)

        for topic in topics:
            await self.toggle_autoread(forum_id, topic.id, enabled)
