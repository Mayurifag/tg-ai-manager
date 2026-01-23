import re

from src.domain.models import Message
from src.users.ports import UserRepository


class RuleCheckerComponent:
    def __init__(self, user_repo: UserRepository):
        self.user_repo = user_repo

    async def check_global_autoread(self, message: Message, unread_count: int) -> str:
        """
        Checks global user settings to see if a message should be auto-read.
        Returns a reason string if true, empty string otherwise.
        """
        # If unread count is high, we might skip heavy regex checks in future optimizations
        # For now, we keep the logic as is.

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
