from typing import Any, Dict, Optional

from src.rules.models import Rule, RuleType
from src.rules.ports import RuleRepository


class RuleCRUDComponent:
    def __init__(self, repo: RuleRepository):
        self.repo = repo

    async def get_rule(
        self, chat_id: int, topic_id: Optional[int], rule_type: RuleType
    ) -> Optional[Rule]:
        rules = await self.repo.get_by_chat_and_topic(chat_id, topic_id)

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

    async def toggle_rule(
        self,
        chat_id: int,
        topic_id: Optional[int],
        rule_type: RuleType,
        enabled: bool,
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[Rule]:
        rules = await self.repo.get_by_chat_and_topic(chat_id, topic_id)
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
                rule_id = await self.repo.add(new_rule)
                new_rule.id = rule_id
                return new_rule
            else:
                if config is not None:
                    existing.config = config
                    await self.repo.update(existing)
                return existing
        else:
            if existing and existing.id:
                await self.repo.delete(existing.id)
            return None
