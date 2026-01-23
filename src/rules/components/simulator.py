from typing import Any, Dict

from src.domain.ports import ChatRepository
from src.rules.components.checker import RuleCheckerComponent
from src.rules.components.crud import RuleCRUDComponent
from src.rules.models import RuleType


class RuleSimulatorComponent:
    def __init__(
        self,
        chat_repo: ChatRepository,
        crud: RuleCRUDComponent,
        checker: RuleCheckerComponent,
    ):
        self.chat_repo = chat_repo
        self.crud = crud
        self.checker = checker

    async def simulate(self, chat_id: int, msg_id: int) -> Dict[str, Any]:
        msgs = await self.chat_repo.get_messages(chat_id, ids=[msg_id])
        msg = msgs[0] if msgs else None

        if not msg:
            return {"error": "Message not found"}

        results = {}

        # 1. Autoread Check
        rule = await self.crud.get_rule(chat_id, None, RuleType.AUTOREAD)
        ar_status = "skipped"
        ar_reason = "disabled"

        if rule:
            ar_status = "would_read"
            ar_reason = "rule_enabled"
        else:
            global_reason = await self.checker.check_global_autoread(
                msg, unread_count=1
            )
            if global_reason:
                ar_status = "would_read"
                ar_reason = global_reason

        results["autoread"] = {"status": ar_status, "reason": ar_reason}

        # 2. Autoreact Check
        react_rule = await self.crud.get_rule(chat_id, None, RuleType.AUTOREACT)
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
