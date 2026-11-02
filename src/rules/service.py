import asyncio
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import regex
from telethon.errors import FloodWaitError

from src.ai.gemini import GeminiClassifier
from src.ai.ports import AIClassifier
from src.domain.errors import AccountChanged
from src.domain.models import ActionLog, ChatType, Message, SystemEvent
from src.domain.ports import ActionRepository, ChatRepository
from src.infrastructure.action_logging import record_action
from src.infrastructure.logging import get_logger
from src.rules.decisions import ReactDecision, ReadDecision
from src.rules.models import DEFAULT_AUTOREACT_EMOJI, Rule, RuleType
from src.rules.ports import RuleRepository
from src.users.models import User
from src.users.ports import UserRepository

logger = get_logger(__name__)

AIClassifierFactory = Callable[[User], AIClassifier]
AI_MAX_INPUT_CHARACTERS = 8000
AI_MAX_CONCURRENT = 4
AI_TIMEOUT_SECONDS = 10


class RuleService:
    def __init__(
        self,
        rule_repo: RuleRepository,
        action_repo: ActionRepository,
        chat_repo: ChatRepository,
        user_repo: UserRepository,
        ai_classifier_factory: Optional[AIClassifierFactory] = None,
    ):
        self.rule_repo = rule_repo
        self.action_repo = action_repo
        self.chat_repo = chat_repo
        self.user_repo = user_repo
        self._ai_classifier_factory = (
            ai_classifier_factory or self._create_ai_classifier
        )
        self._ai_classifier: Optional[AIClassifier] = None
        self._ai_classifier_key: Optional[Tuple[str, str, Optional[str]]] = None
        self._retired_ai_classifiers: list[AIClassifier] = []
        self._ai_active = 0

        # Cache for deduplicating album reactions: (chat_id, grouped_id) -> timestamp
        self._album_reaction_cache: Dict[Tuple[int, int], float] = {}
        self._regex_slots = asyncio.Semaphore(4)
        self._account_generation = 0

    async def retire_account(self) -> None:
        """Invalidate in-flight decisions before closing the cached AI client."""
        self._account_generation += 1
        await self.invalidate_ai(close_active=True)
        self._album_reaction_cache.clear()

    async def invalidate_ai(self, *, close_active: bool = False) -> None:
        if self._ai_classifier:
            self._retired_ai_classifiers.append(self._ai_classifier)
        self._ai_classifier = None
        self._ai_classifier_key = None
        if close_active or self._ai_active == 0:
            await self._close_retired_ai_classifiers()

    async def _close_retired_ai_classifiers(self) -> None:
        retired, self._retired_ai_classifiers = self._retired_ai_classifiers, []
        for classifier in retired:
            close = getattr(classifier, "close", None)
            if close:
                try:
                    await close()
                except Exception as exc:
                    logger.warning(
                        "ai_client_close_failed", category=type(exc).__name__
                    )

    def _create_ai_classifier(self, user: User) -> AIClassifier:
        key = (user.ai_api_key or "", user.ai_model or "", user.ai_prompt)
        if self._ai_classifier_key != key or self._ai_classifier is None:
            classifier = GeminiClassifier(
                api_key=key[0],
                model=key[1],
                prompt=key[2],
            )
            if self._ai_classifier:
                self._retired_ai_classifiers.append(self._ai_classifier)
            self._ai_classifier = classifier
            self._ai_classifier_key = key
        return self._ai_classifier

    async def get_rule(
        self, chat_id: int, topic_id: Optional[int], rule_type: RuleType
    ) -> Optional[Rule]:
        rules = await self.rule_repo.get_by_chat_and_topic(chat_id, topic_id)
        return self._effective_rule(rules, topic_id, rule_type)

    @staticmethod
    def _effective_rule(
        rules: list[Rule], topic_id: Optional[int], rule_type: RuleType
    ) -> Optional[Rule]:
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

    async def is_autoread_enabled(
        self, chat_id: int, topic_id: Optional[int] = None
    ) -> bool:
        rule = await self.get_rule(chat_id, topic_id, RuleType.AUTOREAD)
        return bool(rule)

    async def check_global_autoread_rules(
        self, message: Message, unread_count: Optional[int] = None
    ) -> str:
        if unread_count is not None and unread_count > 1:
            return ""

        user = await self.user_repo.get_user(1)
        if not user:
            return ""
        return await self._match_global_autoread(user, message)

    async def _match_global_autoread(self, user: User, message: Message) -> str:
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

        raw_text = message.raw_text if message.raw_text is not None else message.text
        if user.autoread_regex and raw_text and len(raw_text) <= 16000:
            try:
                async with self._regex_slots:
                    matched = await asyncio.to_thread(
                        regex.search,
                        user.autoread_regex,
                        raw_text,
                        flags=regex.IGNORECASE | regex.VERSION0,
                        timeout=0.05,
                    )
                if matched:
                    return "global_regex"
            except (regex.error, TimeoutError):
                logger.warning("autoread_regex_failed_or_timed_out")

        return ""

    async def handle_new_message_event(self, event: SystemEvent):
        # Strict type guard: we can only process events with a valid chat_id
        if not event.chat_id or not event.scope_known:
            return

        if event.type not in ("message", "action") or not event.message_model:
            return

        msg = event.message_model
        adapter = self.chat_repo
        generation = self._account_generation
        snapshot = await self._read_snapshot(event.chat_id, event.topic_id)
        if self.chat_repo is not adapter or self._account_generation != generation:
            return
        decision = await self.get_read_decision(event, msg, snapshot)
        if self.chat_repo is not adapter or self._account_generation != generation:
            return

        if decision.should_read:
            await self._queue_automatic_read(
                adapter,
                generation,
                snapshot,
                event.chat_id,
                event.topic_id,
                decision.max_id,
                reason=decision.reason,
                chat_name=event.chat_name,
                link=event.link,
                event=event,
            )

        # --- AutoReact Logic ---
        if self.chat_repo is adapter and self._account_generation == generation:
            await self.apply_autoreact(
                event.chat_id,
                event.topic_id,
                msg,
                snapshot=snapshot,
                adapter=adapter,
                generation=generation,
            )

    async def get_read_decision(
        self,
        event: SystemEvent,
        msg: Message,
        snapshot: tuple[Optional[User], list[Rule]],
        *,
        evaluate_ai: bool = True,
    ) -> ReadDecision:
        user, rules = snapshot
        generation, adapter = self._account_generation, self.chat_repo
        if not user:
            return ReadDecision(False, "no_account")
        if any(rule.rule_type == RuleType.AUTOREAD for rule in rules):
            return ReadDecision(True, "autoread_rule", max_id=msg.id)

        # Outgoing messages are not unread incoming messages. Their presence at
        # the top of a dialog cannot authorize clearing an older unread message.
        if msg.is_outgoing:
            return ReadDecision(False, "outgoing")

        reason = await self._match_global_autoread(user, msg)
        if reason:
            return ReadDecision(True, reason, max_id=msg.id)

        reason = ""
        if (
            self.chat_repo is adapter
            and self._account_generation == generation
            and any(rule.rule_type == RuleType.AI_AUTOREAD for rule in rules)
        ):
            if not evaluate_ai:
                return ReadDecision(False, "ai_not_evaluated")
            reason = await self._get_ai_read_reason(event, msg, snapshot)
        if reason:
            return ReadDecision(True, reason, max_id=msg.id)

        return ReadDecision(False, "no_match_or_ai_unavailable")

    async def _read_snapshot(
        self, chat_id: int, topic_id: Optional[int]
    ) -> tuple[Optional[User], list[Rule]]:
        user = await self.user_repo.get_user(1)
        rules = await self.rule_repo.get_by_chat_and_topic(chat_id, topic_id)
        return deepcopy(user), deepcopy(rules)

    async def _queue_automatic_read(
        self,
        adapter: ChatRepository,
        generation: int,
        snapshot: tuple[Optional[User], list[Rule]],
        chat_id: int,
        topic_id: Optional[int],
        max_id: Optional[int],
        *,
        reason: str,
        chat_name: str,
        link: Optional[str],
        event: Optional[SystemEvent] = None,
        action: str = "autoread",
    ) -> bool:
        user, rules = snapshot
        selective = reason not in {
            "autoread_rule",
            "autoread_rule_startup",
            "autoread_reconcile",
        }
        if (
            self.chat_repo is not adapter
            or self._account_generation != generation
            or not user
            or type(max_id) is not int
            or max_id <= 0
        ):
            return False
        required = (
            RuleType.AUTOREAD
            if not selective
            else (RuleType.AI_AUTOREAD if reason == "ai_ad_detected" else None)
        )
        if required and not any(rule.rule_type == required for rule in rules):
            return False

        async def validate() -> bool:
            if self.chat_repo is not adapter or self._account_generation != generation:
                return False
            current = await self._read_snapshot(chat_id, topic_id)
            return (
                self.chat_repo is adapter
                and self._account_generation == generation
                and current == snapshot
            )

        async def on_success() -> None:
            if self.chat_repo is not adapter or self._account_generation != generation:
                return
            if event:
                event.is_read = True
            await record_action(
                self.action_repo,
                ActionLog(
                    action=action,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    reason=reason,
                    date=datetime.now(),
                    link=link,
                ),
            )

        await adapter.mark_automatic_as_read(
            chat_id, topic_id, max_id, validate, on_success, selective=selective
        )
        return True

    async def _get_ai_read_reason(
        self,
        event: SystemEvent,
        msg: Message,
        snapshot: tuple[Optional[User], list[Rule]],
    ) -> str:
        user = snapshot[0]
        raw_text = msg.raw_text if msg.raw_text is not None else msg.text
        if not user or not user.ai_api_key or not user.ai_model or not raw_text:
            return ""
        if (
            len(raw_text) > AI_MAX_INPUT_CHARACTERS
            or self._ai_active >= AI_MAX_CONCURRENT
        ):
            logger.warning(
                "ai_classification_skipped",
                reason="input_or_capacity_limit",
                chat_id=event.chat_id,
            )
            return ""
        generation, adapter = self._account_generation, self.chat_repo
        if await self._read_snapshot(event.chat_id, event.topic_id) != snapshot:
            return ""
        if (
            generation != self._account_generation
            or adapter is not self.chat_repo
            or self._ai_active >= AI_MAX_CONCURRENT
        ):
            return ""

        self._ai_active += 1
        try:
            classifier = self._ai_classifier_factory(user)
            if (
                await asyncio.wait_for(
                    classifier.classify_is_ad(raw_text), AI_TIMEOUT_SECONDS
                )
                is True
            ):
                return "ai_ad_detected"
        except Exception as e:
            logger.warning(
                "ai_classification_failed",
                chat_id=event.chat_id,
                category=type(e).__name__,
            )
        finally:
            self._ai_active -= 1
            if self._ai_active == 0:
                await self._close_retired_ai_classifiers()
        return ""

    async def apply_autoreact(
        self,
        chat_id: int,
        topic_id: Optional[int],
        message: Message,
        *,
        snapshot: Optional[tuple[Optional[User], list[Rule]]] = None,
        adapter: Optional[ChatRepository] = None,
        generation: Optional[int] = None,
    ):
        adapter = adapter or self.chat_repo
        if generation is None:
            generation = self._account_generation
        if snapshot is None:
            snapshot = await self._read_snapshot(chat_id, topic_id)
        if not snapshot[0]:
            return
        rule = self._effective_rule(snapshot[1], topic_id, RuleType.AUTOREACT)
        decision = self._react_decision(rule, message)
        if (
            not decision.should_react
            or self.chat_repo is not adapter
            or self._account_generation != generation
        ):
            return

        album_key = None
        if message.grouped_id:
            now = time.monotonic()
            self._album_reaction_cache = {
                k: v for k, v in self._album_reaction_cache.items() if now - v < 60
            }
            if len(self._album_reaction_cache) > 500:
                self._album_reaction_cache = dict(
                    sorted(self._album_reaction_cache.items(), key=lambda x: x[1])[
                        -250:
                    ]
                )

            album_key = (chat_id, message.grouped_id)
            if album_key in self._album_reaction_cache:
                return
            self._album_reaction_cache[album_key] = now

        async def validate() -> bool:
            if self.chat_repo is not adapter or self._account_generation != generation:
                return False
            current = await self._read_snapshot(chat_id, topic_id)
            return (
                self.chat_repo is adapter
                and self._account_generation == generation
                and current == snapshot
            )

        async def on_complete(success: bool) -> None:
            if album_key and self._account_generation == generation:
                if success:
                    self._album_reaction_cache[album_key] = time.monotonic()
                else:
                    self._album_reaction_cache.pop(album_key, None)

        try:
            accepted = await adapter.send_automatic_reaction(
                chat_id, message.id, decision.emoji, validate, on_complete
            )
        except BaseException:
            if album_key:
                self._album_reaction_cache.pop(album_key, None)
            raise
        if not accepted and album_key:
            self._album_reaction_cache.pop(album_key, None)

    async def get_react_decision(
        self, chat_id: int, topic_id: Optional[int], message: Message
    ) -> ReactDecision:
        rule = await self.get_rule(chat_id, topic_id, RuleType.AUTOREACT)
        return self._react_decision(rule, message)

    @staticmethod
    def _react_decision(rule: Optional[Rule], message: Message) -> ReactDecision:
        if message.is_outgoing:
            return ReactDecision(False, reason="outgoing")
        if message.is_service:
            return ReactDecision(False, reason="service_message")
        if not rule:
            return ReactDecision(False, reason="no_rule")

        emoji = rule.config.get("emoji", DEFAULT_AUTOREACT_EMOJI)
        target_users = rule.config.get("target_users", [])

        if target_users and message.sender_id not in target_users:
            return ReactDecision(False, reason="sender_mismatch")

        for reaction in message.reactions:
            if reaction.is_chosen and (
                reaction.emoji == emoji
                or (reaction.custom_emoji_id and str(reaction.custom_emoji_id) == emoji)
            ):
                return ReactDecision(False, reason="already_chosen")

        return ReactDecision(True, emoji, "rule_enabled")

    async def run_startup_scan(self):
        adapter = self.chat_repo
        generation = self._account_generation
        if not adapter.is_connected():
            return

        try:
            unread_chats = await adapter.get_all_unread_chats()
            total = len(unread_chats)
            queued = 0

            logger.info(
                "startup_scan_started",
                total_unread_chats=total,
            )

            for chat in unread_chats:
                try:
                    if (
                        self.chat_repo is not adapter
                        or self._account_generation != generation
                    ):
                        break
                    if chat.unread_count == 0:
                        continue

                    if chat.type == ChatType.FORUM:
                        topics = await adapter.get_unread_topics(chat.id)
                        for topic in topics:
                            snapshot = await self._read_snapshot(chat.id, topic.id)
                            if any(
                                rule.rule_type == RuleType.AUTOREAD
                                for rule in snapshot[1]
                            ):
                                queued += await self._queue_automatic_read(
                                    adapter,
                                    generation,
                                    snapshot,
                                    chat.id,
                                    topic.id,
                                    topic.last_message_id,
                                    reason="autoread_rule_startup",
                                    chat_name=f"{chat.name} (Topic {topic.name})",
                                    link=f"/forum/{chat.id}",
                                    action="startup_read",
                                )
                    else:
                        snapshot = await self._read_snapshot(chat.id, None)
                        reason = ""
                        max_id = chat.last_message_id
                        if any(
                            rule.rule_type == RuleType.AUTOREAD for rule in snapshot[1]
                        ):
                            reason = "autoread_rule_startup"
                        elif chat.unread_count == 1:
                            msgs = await adapter.get_messages(chat.id, limit=1)
                            if msgs and not msgs[0].is_outgoing:
                                msg = msgs[0]
                                if snapshot[0]:
                                    reason = await self._match_global_autoread(
                                        snapshot[0], msg
                                    )
                                max_id = msg.id
                        if reason:
                            queued += await self._queue_automatic_read(
                                adapter,
                                generation,
                                snapshot,
                                chat.id,
                                None,
                                max_id,
                                reason=reason,
                                chat_name=chat.name,
                                link=f"/chat/{chat.id}",
                                action="startup_read",
                            )
                except Exception as e:
                    logger.warning(
                        "startup_scan_chat_failed",
                        chat_id=chat.id,
                        chat_name=chat.name,
                        error=repr(e),
                    )

            logger.info(
                "startup_scan_completed",
                chats_processed=total,
                reads_queued=queued,
            )
        except Exception as e:
            logger.warning("startup_scan_failed", error=repr(e))

    async def reconcile_autoread(self) -> None:
        """Repair missed updates only for explicitly enabled auto-read scopes."""
        adapter = self.chat_repo
        generation = self._account_generation
        rules = [
            rule
            for rule in await self.rule_repo.get_all()
            if rule.user_id == 1 and rule.rule_type == RuleType.AUTOREAD
        ]
        if not rules or not adapter.is_connected():
            return

        chat_ids = sorted({rule.chat_id for rule in rules})
        unread_chats = await adapter.get_all_unread_chats(chat_ids)
        for chat in unread_chats:
            if self.chat_repo is not adapter or self._account_generation != generation:
                break
            if chat.type == ChatType.FORUM:
                chat_rules = [rule for rule in rules if rule.chat_id == chat.id]
                topic_ids = (
                    None
                    if any(rule.topic_id is None for rule in chat_rules)
                    else [
                        rule.topic_id
                        for rule in chat_rules
                        if rule.topic_id is not None
                    ]
                )
                try:
                    topics = await adapter.get_unread_topics(chat.id, topic_ids)
                except FloodWaitError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "reconcile_forum_failed",
                        chat_id=chat.id,
                        category=type(exc).__name__,
                    )
                    continue
                for topic in topics:
                    snapshot = await self._read_snapshot(chat.id, topic.id)
                    if any(rule.rule_type == RuleType.AUTOREAD for rule in snapshot[1]):
                        await self._queue_automatic_read(
                            adapter,
                            generation,
                            snapshot,
                            chat.id,
                            topic.id,
                            topic.last_message_id,
                            reason="autoread_reconcile",
                            chat_name=f"{chat.name} (Topic {topic.name})",
                            link=f"/forum/{chat.id}",
                        )
            else:
                snapshot = await self._read_snapshot(chat.id, None)
                if any(rule.rule_type == RuleType.AUTOREAD for rule in snapshot[1]):
                    await self._queue_automatic_read(
                        adapter,
                        generation,
                        snapshot,
                        chat.id,
                        None,
                        chat.last_message_id,
                        reason="autoread_reconcile",
                        chat_name=chat.name,
                        link=f"/chat/{chat.id}",
                    )

    async def toggle_ai_autoread(
        self, chat_id: int, topic_id: Optional[int], enabled: bool
    ) -> Optional[Rule]:
        return await self._toggle_rule(chat_id, topic_id, RuleType.AI_AUTOREAD, enabled)

    async def is_ai_autoread_enabled(
        self, chat_id: int, topic_id: Optional[int] = None
    ) -> bool:
        rule = await self.get_rule(chat_id, topic_id, RuleType.AI_AUTOREAD)
        return bool(rule)

    async def toggle_autoread(
        self, chat_id: int, topic_id: Optional[int], enabled: bool
    ) -> Optional[Rule]:
        return await self._toggle_rule(chat_id, topic_id, RuleType.AUTOREAD, enabled)

    async def set_autoreact(
        self,
        chat_id: int,
        topic_id: Optional[int],
        enabled: bool,
        config: Dict[str, Any],
    ) -> Optional[Rule]:
        return await self._toggle_rule(
            chat_id, topic_id, RuleType.AUTOREACT, enabled, config
        )

    async def _toggle_rule(
        self,
        chat_id: int,
        topic_id: Optional[int],
        rule_type: RuleType,
        enabled: bool,
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[Rule]:
        return await self.rule_repo.set_scope(
            Rule(
                user_id=1,
                rule_type=rule_type,
                chat_id=chat_id,
                topic_id=topic_id,
                config=config or {},
            ),
            enabled,
            update_config=config is not None,
        )

    async def apply_autoread_to_all_topics(self, forum_id: int, enabled: bool):
        await self.rule_repo.set_forum_autoread(1, forum_id, enabled)

    async def simulate_process_message(
        self,
        chat_id: int,
        msg_id: int,
        *,
        topic_id: Optional[int] = None,
        evaluate_ai: bool = False,
    ) -> Dict[str, Any]:
        """Evaluate the production decisions without enqueuing Telegram writes."""
        adapter, generation = self.chat_repo, self._account_generation

        def ensure_current() -> None:
            if self.chat_repo is not adapter or self._account_generation != generation:
                raise AccountChanged()

        context = await adapter.get_message_context(chat_id, msg_id)
        ensure_current()
        if not context:
            return {"error": "Message not found"}
        if not context.scope_known:
            return {"error": "Message topic is unknown"}
        if topic_id is not None and topic_id != context.topic_id:
            return {"error": "Message does not belong to this topic"}
        topic_id, msg = context.topic_id, context.message
        snapshot = await self._read_snapshot(chat_id, topic_id)
        ensure_current()
        event = SystemEvent(
            type="action" if msg.is_service else "message",
            text=msg.text,
            chat_name="",
            chat_id=chat_id,
            topic_id=topic_id,
            message_model=msg,
        )
        read = await self.get_read_decision(
            event, msg, snapshot, evaluate_ai=evaluate_ai
        )
        ensure_current()
        read_rule = None
        if read.reason == "autoread_rule":
            read_rule = self._effective_rule(snapshot[1], topic_id, RuleType.AUTOREAD)
        elif read.reason in {"ai_ad_detected", "ai_not_evaluated"}:
            read_rule = self._effective_rule(
                snapshot[1], topic_id, RuleType.AI_AUTOREAD
            )
        read_result: Dict[str, Any] = {
            "status": "would_read" if read.should_read else "skipped",
            "reason": read.reason,
            "inherited": bool(
                topic_id is not None and read_rule and read_rule.topic_id is None
            ),
            "safety": "not_applicable"
            if read.reason == "autoread_rule"
            else "not_checked",
        }
        if read.reason == "ai_not_evaluated":
            read_result["status"] = "not_evaluated"
        elif read.should_read and read.reason != "autoread_rule":
            allowed = await adapter.can_selectively_read(chat_id, topic_id, msg.id)
            ensure_current()
            read_result["safety"] = "passed_current_state" if allowed else "vetoed"
            if not allowed:
                read_result.update(
                    status="blocked",
                    matched_reason=read.reason,
                    reason="unread_safety_veto",
                )

        react_rule = self._effective_rule(snapshot[1], topic_id, RuleType.AUTOREACT)
        react = self._react_decision(react_rule, msg)
        if not snapshot[0]:
            react = ReactDecision(False, reason="no_account")
        elif react.should_react and msg.grouped_id:
            last = self._album_reaction_cache.get((chat_id, msg.grouped_id))
            if last is not None and time.monotonic() - last < 60:
                react = ReactDecision(False, reason="album_already_pending_or_reacted")

        return {
            "scope": {"chat_id": chat_id, "topic_id": topic_id},
            "autoread": read_result,
            "autoreact": {
                "status": "would_react" if react.should_react else "skipped",
                "reason": react.reason,
                "detail": f"emoji: {react.emoji}"
                if react.should_react
                else react.reason,
                "inherited": bool(
                    topic_id is not None and react_rule and react_rule.topic_id is None
                ),
            },
            "note": "Current state only; queued actions are revalidated before execution.",
        }
