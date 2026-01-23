import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from src.domain.models import ActionLog, Message, SystemEvent
from src.domain.ports import ActionRepository, ChatRepository
from src.infrastructure.queue_service import QueueService
from src.rules.components.checker import RuleCheckerComponent
from src.rules.components.crud import RuleCRUDComponent
from src.rules.components.scanner import StartupScannerComponent
from src.rules.components.simulator import RuleSimulatorComponent
from src.rules.models import Rule, RuleType
from src.rules.ports import RuleRepository
from src.users.ports import UserRepository


class RuleService:
    """
    Facade for Rule Sub-components.
    Orchestrates the application logic for AutoRead/AutoReact events.
    """

    def __init__(
        self,
        rule_repo: RuleRepository,
        action_repo: ActionRepository,
        chat_repo: ChatRepository,
        user_repo: UserRepository,
        queue_service: QueueService,
    ):
        self.action_repo = action_repo
        self.queue_service = queue_service
        self.chat_repo = chat_repo  # Exposed for container re-injection if needed

        # Initialize Components
        self.crud = RuleCRUDComponent(rule_repo)
        self.checker = RuleCheckerComponent(user_repo)
        self.scanner = StartupScannerComponent(
            chat_repo, queue_service, action_repo, self.crud, self.checker
        )
        self.simulator = RuleSimulatorComponent(chat_repo, self.crud, self.checker)

        # Cache for deduplicating album reactions: (chat_id, grouped_id) -> timestamp
        self._album_reaction_cache: Dict[Tuple[int, int], float] = {}

    # --- Facade Methods for Web API ---

    async def get_rule(
        self, chat_id: int, topic_id: Optional[int], rule_type: RuleType
    ) -> Optional[Rule]:
        return await self.crud.get_rule(chat_id, topic_id, rule_type)

    async def is_autoread_enabled(
        self, chat_id: int, topic_id: Optional[int] = None
    ) -> bool:
        rule = await self.crud.get_rule(chat_id, topic_id, RuleType.AUTOREAD)
        return bool(rule)

    async def toggle_autoread(
        self, chat_id: int, topic_id: Optional[int], enabled: bool
    ) -> Optional[Rule]:
        return await self.crud.toggle_rule(
            chat_id, topic_id, RuleType.AUTOREAD, enabled
        )

    async def set_autoreact(
        self,
        chat_id: int,
        topic_id: Optional[int],
        enabled: bool,
        config: Dict[str, Any],
    ) -> Optional[Rule]:
        return await self.crud.toggle_rule(
            chat_id, topic_id, RuleType.AUTOREACT, enabled, config
        )

    async def apply_autoread_to_all_topics(self, forum_id: int, enabled: bool):
        await self.toggle_autoread(forum_id, None, enabled)
        topics = await self.chat_repo.get_forum_topics(forum_id)
        for topic in topics:
            await self.toggle_autoread(forum_id, topic.id, enabled)

    async def run_startup_scan(self):
        await self.scanner.run()

    async def simulate_process_message(
        self, chat_id: int, msg_id: int
    ) -> Dict[str, Any]:
        return await self.simulator.simulate(chat_id, msg_id)

    # --- Event Processing Logic ---

    async def handle_new_message_event(self, event: SystemEvent):
        if not event.chat_id or event.type != "message" or not event.message_model:
            return

        msg = event.message_model

        # 1. AutoRead
        should_read = False
        reason = ""

        if await self.is_autoread_enabled(event.chat_id, event.topic_id):
            should_read = True
            reason = "autoread_rule"

        if not should_read:
            # Assume unread=1 for new event
            reason = await self.checker.check_global_autoread(msg, unread_count=1)
            if reason:
                # Double check real state if possible? (Skipped for performance on event loop)
                should_read = True

        if should_read:
            await self.queue_service.enqueue_mark_read(
                event.chat_id, event.topic_id, max_id=msg.id
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

        # 2. AutoReact
        await self._apply_autoreact(event.chat_id, event.topic_id, msg)

    async def _apply_autoreact(
        self, chat_id: int, topic_id: Optional[int], message: Message
    ):
        if message.is_outgoing:
            return

        # Album Deduplication
        if message.grouped_id:
            now = time.time()
            self._album_reaction_cache = {
                k: v for k, v in self._album_reaction_cache.items() if now - v < 60
            }
            key = (chat_id, message.grouped_id)
            if key in self._album_reaction_cache:
                return
            self._album_reaction_cache[key] = now

        rule = await self.crud.get_rule(chat_id, topic_id, RuleType.AUTOREACT)
        if not rule:
            return

        emoji = rule.config.get("emoji", "💩")
        target_users = rule.config.get("target_users", [])

        should_react = False
        if not target_users:
            should_react = True
        elif message.sender_id in target_users:
            should_react = True

        if should_react:
            # Check existing reactions
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
