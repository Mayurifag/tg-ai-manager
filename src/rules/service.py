from typing import List, Optional
from src.rules.models import Rule, AutoReadRule, RuleType
from src.rules.ports import RuleRepository
from src.domain.models import SystemEvent, ActionLog
from src.domain.ports import ActionRepository, ChatRepository
from datetime import datetime

class RuleService:
    def __init__(self, rule_repo: RuleRepository, action_repo: ActionRepository, chat_repo: ChatRepository):
        self.rule_repo = rule_repo
        self.action_repo = action_repo
        self.chat_repo = chat_repo

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

    async def handle_new_message_event(self, event: SystemEvent):
        if event.type != "message" or not event.message_model:
            return

        if event.message_model.is_service:
            return

        if await self.is_autoread_enabled(event.chat_id, event.topic_id):
            # Perform actual read operation
            await self.chat_repo.mark_as_read(event.chat_id, event.topic_id)

            chat_name = event.chat_name

            log = ActionLog(
                action="autoread",
                chat_id=event.chat_id,
                chat_name=chat_name,
                reason="autoread_rule",
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
