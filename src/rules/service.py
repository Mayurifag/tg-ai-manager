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
        return any(r.rule_type == RuleType.AUTOREAD and r.enabled for r in rules)

    async def handle_new_message_event(self, event: SystemEvent):
        if event.type != "message" or not event.message_model:
            return

        if event.message_model.is_service:
            return

        if await self.is_autoread_enabled(event.chat_id, event.topic_id):
            chat_name = event.chat_name

            log = ActionLog(
                action="would_autoread",
                chat_id=event.chat_id,
                chat_name=chat_name,
                reason="autoread_rule",
                date=datetime.now(),
                link=event.link
            )
            await self.action_repo.add_log(log)

    async def toggle_autoread(self, chat_id: int, topic_id: Optional[int], enabled: bool) -> Rule:
        rules = await self.rule_repo.get_by_chat_and_topic(chat_id, topic_id)
        autoread_rule = next((r for r in rules if r.rule_type == RuleType.AUTOREAD), None)

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
