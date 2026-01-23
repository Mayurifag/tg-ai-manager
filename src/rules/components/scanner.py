import asyncio
from datetime import datetime

from src.domain.models import ActionLog, ChatType
from src.domain.ports import ActionRepository, ChatRepository
from src.infrastructure.queue_service import QueueService
from src.rules.components.checker import RuleCheckerComponent
from src.rules.components.crud import RuleCRUDComponent
from src.rules.models import RuleType


class StartupScannerComponent:
    def __init__(
        self,
        chat_repo: ChatRepository,
        queue_service: QueueService,
        action_repo: ActionRepository,
        crud: RuleCRUDComponent,
        checker: RuleCheckerComponent,
    ):
        self.chat_repo = chat_repo
        self.queue_service = queue_service
        self.action_repo = action_repo
        self.crud = crud
        self.checker = checker

    async def run(self):
        if (
            not hasattr(self.chat_repo, "is_connected")
            or not self.chat_repo.is_connected()
        ):
            return

        try:
            unread_chats = await self.chat_repo.get_all_unread_chats()
            for chat in unread_chats:
                try:
                    await self._process_chat(chat)
                    # Yield control to avoid blocking event loop
                    await asyncio.sleep(0.05)
                except Exception:
                    pass
        except Exception:
            pass

    async def _process_chat(self, chat):
        if chat.type == ChatType.FORUM:
            topics = await self.chat_repo.get_unread_topics(chat.id)
            for topic in topics:
                rule = await self.crud.get_rule(chat.id, topic.id, RuleType.AUTOREAD)
                if rule:
                    await self.queue_service.enqueue_mark_read(chat.id, topic.id)
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

            # Check Rule
            rule = await self.crud.get_rule(chat.id, None, RuleType.AUTOREAD)
            if rule:
                should_read = True
                reason = "autoread_rule_startup"

            # Check Global (only if unread count is low to avoid reading massive history unexpectedly)
            elif chat.unread_count == 1:
                msgs = await self.chat_repo.get_messages(chat.id, limit=1)
                if msgs:
                    reason = await self.checker.check_global_autoread(
                        msgs[0], chat.unread_count
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
