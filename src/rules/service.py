import asyncio
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from weakref import WeakValueDictionary

import regex
from telethon.errors import FloodWaitError

from src.ai.gemini import GeminiClassifier
from src.ai.ports import AIClassifier
from src.domain.errors import AccountChanged
from src.domain.models import ActionLog, ChatType, Message, SystemEvent
from src.domain.ports import ActionRepository, ChatRepository
from src.infrastructure.action_logging import record_action
from src.infrastructure.logging import get_logger
from src.infrastructure.tasks import (
    raise_if_task_stopped,
    run_with_task_deadline,
)
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
# shortcut: give retired AI clients three seconds to close; revisit if normal
# provider cleanup routinely needs longer.
AI_CLOSE_TIMEOUT_SECONDS = 3


@dataclass
class _AIPolicyEpoch:
    revision: int = 0
    changes: int = 0


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
        self._ai_policy_global = _AIPolicyEpoch()
        self._ai_policy_chats: WeakValueDictionary[int, _AIPolicyEpoch] = (
            WeakValueDictionary()
        )

        # Pending work does not expire while waiting in the Telegram queue.
        self._album_reaction_pending: Dict[Tuple[int, int], object] = {}
        # Completed reactions are retained briefly to suppress replayed events.
        self._album_reaction_cache: Dict[Tuple[int, int], float] = {}
        self._regex_slots = asyncio.Semaphore(4)
        self._account_generation = 0

    async def retire_account(self) -> None:
        """Invalidate in-flight decisions before closing the cached AI client."""
        self._account_generation += 1
        await self.invalidate_ai(close_active=True)
        self._album_reaction_pending.clear()
        self._album_reaction_cache.clear()

    async def invalidate_ai(self, *, close_active: bool = False) -> None:
        self._ai_policy_global.revision += 1
        if self._ai_classifier:
            self._retired_ai_classifiers.append(self._ai_classifier)
        self._ai_classifier = None
        self._ai_classifier_key = None
        if close_active or self._ai_active == 0:
            await self._close_retired_ai_classifiers()

    def _ai_policy_for_chat(self, chat_id: int) -> _AIPolicyEpoch:
        policy = self._ai_policy_chats.get(chat_id)
        if policy is None:
            policy = _AIPolicyEpoch()
            self._ai_policy_chats[chat_id] = policy
        return policy

    @contextmanager
    def ai_policy_change(self, chat_id: Optional[int] = None) -> Iterator[None]:
        """Invalidate AI decisions across an in-progress policy mutation."""
        policy = (
            self._ai_policy_global
            if chat_id is None
            else self._ai_policy_for_chat(chat_id)
        )
        policy.revision += 1
        policy.changes += 1
        try:
            yield
        finally:
            policy.changes -= 1
            policy.revision += 1

    def _capture_ai_policy(
        self, chat_id: int, adapter: ChatRepository, generation: int
    ) -> Callable[[], bool]:
        global_policy = self._ai_policy_global
        chat_policy = self._ai_policy_for_chat(chat_id)
        global_revision, chat_revision = global_policy.revision, chat_policy.revision

        def is_current() -> bool:
            return (
                generation == self._account_generation
                and adapter is self.chat_repo
                and not getattr(adapter, "_retired", False)
                and not global_policy.changes
                and not chat_policy.changes
                and global_policy.revision == global_revision
                and chat_policy.revision == chat_revision
            )

        return is_current

    async def _close_retired_ai_classifiers(self) -> None:
        retired, self._retired_ai_classifiers = self._retired_ai_classifiers, []
        started = [False] * len(retired)

        async def close_classifier(index: int, classifier: AIClassifier) -> None:
            started[index] = True
            close = getattr(classifier, "close", None)
            if close:
                try:
                    await close()
                except Exception as exc:
                    logger.warning(
                        "ai_client_close_failed", category=type(exc).__name__
                    )

        if not retired:
            return
        tasks = [
            asyncio.create_task(close_classifier(index, client))
            for index, client in enumerate(retired)
        ]
        try:
            async with asyncio.timeout(AI_CLOSE_TIMEOUT_SECONDS):
                await asyncio.gather(*tasks)
        except TimeoutError:
            logger.warning("ai_client_close_timed_out", count=len(retired))
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            raise
        finally:
            # Cancellation can reach gather before its children ever run. Keep
            # ownership of clients whose close was not even attempted so a
            # later retirement can retry them.
            self._retired_ai_classifiers.extend(
                client for index, client in enumerate(retired) if not started[index]
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
        raise_if_task_stopped()
        # Strict type guard: we can only process events with a valid chat_id
        if not event.chat_id or not event.scope_known:
            return

        if event.type not in ("message", "action") or not event.message_model:
            return

        msg = event.message_model
        adapter = self.chat_repo
        generation = self._account_generation
        ai_policy_current = self._capture_ai_policy(event.chat_id, adapter, generation)

        def check_account() -> None:
            self._ensure_account_current(adapter, generation)

        try:
            snapshot = await self._read_snapshot(
                event.chat_id, event.topic_id, check_current=check_account
            )
        except AccountChanged:
            return
        raise_if_task_stopped()
        if self.chat_repo is not adapter or self._account_generation != generation:
            return
        decision = await self.get_read_decision(
            event, msg, snapshot, ai_policy_current=ai_policy_current
        )
        raise_if_task_stopped()
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
                ai_policy_current=ai_policy_current
                if decision.reason == "ai_ad_detected"
                else None,
            )
            raise_if_task_stopped()

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
            raise_if_task_stopped()

    async def get_read_decision(
        self,
        event: SystemEvent,
        msg: Message,
        snapshot: tuple[Optional[User], list[Rule]],
        *,
        evaluate_ai: bool = True,
        ai_policy_current: Optional[Callable[[], bool]] = None,
    ) -> ReadDecision:
        raise_if_task_stopped()
        user, rules = snapshot
        generation, adapter = self._account_generation, self.chat_repo
        if evaluate_ai and ai_policy_current is None and event.chat_id:
            ai_policy_current = self._capture_ai_policy(
                event.chat_id, adapter, generation
            )
        if not user:
            return ReadDecision(False, "no_account")
        if any(rule.rule_type == RuleType.AUTOREAD for rule in rules):
            return ReadDecision(True, "autoread_rule", max_id=msg.id)

        # Outgoing messages are not unread incoming messages. Their presence at
        # the top of a dialog cannot authorize clearing an older unread message.
        if msg.is_outgoing:
            return ReadDecision(False, "outgoing")

        reason = await self._match_global_autoread(user, msg)
        raise_if_task_stopped()
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
            reason = await self._get_ai_read_reason(
                event, msg, snapshot, ai_policy_current=ai_policy_current
            )
        if reason:
            return ReadDecision(True, reason, max_id=msg.id)

        return ReadDecision(False, "no_match_or_ai_unavailable")

    async def _read_snapshot(
        self,
        chat_id: int,
        topic_id: Optional[int],
        *,
        check_current: Optional[Callable[[], None]] = None,
    ) -> tuple[Optional[User], list[Rule]]:
        check = check_current or raise_if_task_stopped
        check()
        try:
            user = await self.user_repo.get_user(1)
            check()
            rules = await self.rule_repo.get_by_chat_and_topic(chat_id, topic_id)
            check()
            return deepcopy(user), deepcopy(rules)
        finally:
            check()

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
        ai_policy_current: Optional[Callable[[], bool]] = None,
    ) -> bool:
        raise_if_task_stopped()
        user, rules = snapshot
        selective = reason not in {
            "autoread_rule",
            "autoread_rule_startup",
            "autoread_reconcile",
        }
        if (
            self.chat_repo is not adapter
            or self._account_generation != generation
            or getattr(adapter, "_retired", False)
            or not user
            or type(max_id) is not int
            or max_id <= 0
            or (
                reason == "ai_ad_detected"
                and (ai_policy_current is None or not ai_policy_current())
            )
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
            if (
                self.chat_repo is not adapter
                or self._account_generation != generation
                or getattr(adapter, "_retired", False)
            ):
                return False
            if ai_policy_current is not None and not ai_policy_current():
                return False

            def check() -> None:
                self._ensure_account_current(adapter, generation)

            try:
                current = await self._read_snapshot(
                    chat_id, topic_id, check_current=check
                )
            except AccountChanged:
                return False
            return (
                self.chat_repo is adapter
                and self._account_generation == generation
                and not getattr(adapter, "_retired", False)
                and (ai_policy_current is None or ai_policy_current())
                and current == snapshot
            )

        async def on_success() -> None:
            if (
                self.chat_repo is not adapter
                or self._account_generation != generation
                or getattr(adapter, "_retired", False)
            ):
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
                    link=link,
                ),
            )

        await adapter.mark_automatic_as_read(
            chat_id, topic_id, max_id, validate, on_success, selective=selective
        )
        raise_if_task_stopped()
        return True

    async def _get_ai_read_reason(
        self,
        event: SystemEvent,
        msg: Message,
        snapshot: tuple[Optional[User], list[Rule]],
        *,
        ai_policy_current: Optional[Callable[[], bool]] = None,
    ) -> str:
        raise_if_task_stopped()
        user = snapshot[0]
        raw_text = msg.raw_text if msg.raw_text is not None else msg.text
        if (
            not event.chat_id
            or not user
            or not user.ai_api_key
            or not user.ai_model
            or not raw_text
        ):
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
        adapter, generation = self.chat_repo, self._account_generation
        authorized = ai_policy_current or self._capture_ai_policy(
            event.chat_id, adapter, generation
        )

        if not authorized():
            return ""

        def check_account() -> None:
            self._ensure_account_current(adapter, generation)

        try:
            current = await self._read_snapshot(
                event.chat_id, event.topic_id, check_current=check_account
            )
        except AccountChanged:
            return ""
        raise_if_task_stopped()
        if current != snapshot:
            return ""
        if not authorized() or self._ai_active >= AI_MAX_CONCURRENT:
            return ""
        self._ai_active += 1
        matched = False
        try:
            classifier = self._ai_classifier_factory(user)
            if authorized() and AI_TIMEOUT_SECONDS > 0:
                deadline = asyncio.get_running_loop().time() + AI_TIMEOUT_SECONDS

                async def classify() -> Optional[bool]:
                    # Starting a child yields control after the caller's check.
                    if not authorized():
                        return None
                    raise_if_task_stopped()
                    return await classifier.classify_is_ad(raw_text)

                result = await run_with_task_deadline(
                    classify, deadline, name="ai_classification"
                )
                matched = result is True
        except TimeoutError:
            logger.warning(
                "ai_classification_skipped", reason="timeout", chat_id=event.chat_id
            )
        except Exception as e:
            logger.warning(
                "ai_classification_failed",
                chat_id=event.chat_id,
                category=type(e).__name__,
            )
        finally:
            self._ai_active -= 1
            try:
                if self._ai_active == 0:
                    await self._close_retired_ai_classifiers()
            finally:
                raise_if_task_stopped()
        return "ai_ad_detected" if matched and authorized() else ""

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
        raise_if_task_stopped()
        adapter = adapter or self.chat_repo
        if generation is None:
            generation = self._account_generation

        def check_account() -> None:
            self._ensure_account_current(adapter, generation)

        if snapshot is None:
            try:
                snapshot = await self._read_snapshot(
                    chat_id, topic_id, check_current=check_account
                )
            except AccountChanged:
                return
            raise_if_task_stopped()
        if not snapshot[0]:
            return
        rule = self._effective_rule(snapshot[1], topic_id, RuleType.AUTOREACT)
        decision = self._react_decision(rule, message)
        if (
            not decision.should_react
            or self.chat_repo is not adapter
            or self._account_generation != generation
            or getattr(adapter, "_retired", False)
        ):
            return

        album_key = None
        album_attempt = None
        if message.grouped_id:
            now = time.monotonic()
            self._album_reaction_cache = {
                k: v for k, v in self._album_reaction_cache.items() if now - v < 60
            }
            album_key = (chat_id, message.grouped_id)
            if (
                album_key in self._album_reaction_pending
                or album_key in self._album_reaction_cache
            ):
                return
            album_attempt = object()
            self._album_reaction_pending[album_key] = album_attempt

        async def validate() -> bool:
            if (
                self.chat_repo is not adapter
                or self._account_generation != generation
                or getattr(adapter, "_retired", False)
            ):
                return False
            try:
                current = await self._read_snapshot(
                    chat_id, topic_id, check_current=check_account
                )
            except AccountChanged:
                return False
            return (
                self.chat_repo is adapter
                and self._account_generation == generation
                and not getattr(adapter, "_retired", False)
                and current == snapshot
            )

        async def on_complete(success: Optional[bool]) -> None:
            raise_if_task_stopped()
            # An unknown outcome must not permit another album part to react.
            if (
                success is not None
                and album_key is not None
                and self.chat_repo is adapter
                and self._account_generation == generation
                and not getattr(adapter, "_retired", False)
                and self._album_reaction_pending.get(album_key) is album_attempt
            ):
                completed_at = time.monotonic() if success else 0.0
                raise_if_task_stopped()
                self._album_reaction_pending.pop(album_key)
                if success:
                    self._album_reaction_cache[album_key] = completed_at
                    if len(self._album_reaction_cache) > 500:
                        self._album_reaction_cache = dict(
                            sorted(
                                self._album_reaction_cache.items(), key=lambda x: x[1]
                            )[-500:]
                        )

        try:
            accepted = await adapter.send_automatic_reaction(
                chat_id, message.id, decision.emoji, validate, on_complete
            )
        except BaseException:
            if (
                album_key is not None
                and self._album_reaction_pending.get(album_key) is album_attempt
            ):
                self._album_reaction_pending.pop(album_key)
            raise
        if (
            not accepted
            and album_key is not None
            and self._album_reaction_pending.get(album_key) is album_attempt
        ):
            self._album_reaction_pending.pop(album_key)
        raise_if_task_stopped()

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
        target_users = rule.config.get("target_users") or []

        if target_users and message.sender_id not in target_users:
            return ReactDecision(False, reason="sender_mismatch")

        for reaction in message.reactions:
            if reaction.is_chosen and (
                reaction.emoji == emoji
                or (reaction.custom_emoji_id and str(reaction.custom_emoji_id) == emoji)
            ):
                return ReactDecision(False, reason="already_chosen")

        return ReactDecision(True, emoji, "rule_enabled")

    def _ensure_account_current(self, adapter: ChatRepository, generation: int) -> None:
        raise_if_task_stopped()
        if (
            self.chat_repo is not adapter
            or self._account_generation != generation
            or getattr(adapter, "_retired", False)
        ):
            raise AccountChanged()

    async def run_startup_scan(self, adapter: Optional[ChatRepository] = None):
        raise_if_task_stopped()
        if adapter is None:
            adapter = self.chat_repo
        if self.chat_repo is not adapter or getattr(adapter, "_retired", False):
            return
        generation = self._account_generation
        if not adapter.is_connected():
            return

        async def scan() -> None:
            try:
                await self._run_startup_scan(adapter, generation)
            except AccountChanged:
                return

        await run_with_task_deadline(scan, None, name="startup_scan_run")

    async def _run_startup_scan(self, adapter: ChatRepository, generation: int) -> None:
        def check() -> None:
            self._ensure_account_current(adapter, generation)

        try:
            check()
            unread_chats = await adapter.get_all_unread_chats()
            check()
            total = len(unread_chats)
            queued = 0

            logger.info(
                "startup_scan_started",
                total_unread_chats=total,
            )

            for chat in unread_chats:
                check()
                try:
                    if chat.unread_count == 0:
                        continue

                    if chat.type == ChatType.FORUM:
                        topics = await adapter.get_unread_topics(chat.id)
                        check()
                        for topic in topics:
                            check()
                            snapshot = await self._read_snapshot(
                                chat.id, topic.id, check_current=check
                            )
                            if any(
                                rule.rule_type == RuleType.AUTOREAD
                                for rule in snapshot[1]
                            ):
                                check()
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
                                check()
                    else:
                        snapshot = await self._read_snapshot(
                            chat.id, None, check_current=check
                        )
                        reason = ""
                        max_id = chat.last_message_id
                        if any(
                            rule.rule_type == RuleType.AUTOREAD for rule in snapshot[1]
                        ):
                            reason = "autoread_rule_startup"
                        elif chat.unread_count == 1:
                            check()
                            msgs = await adapter.get_messages(chat.id, limit=1)
                            check()
                            if msgs and not msgs[0].is_outgoing:
                                msg = msgs[0]
                                if snapshot[0]:
                                    reason = await self._match_global_autoread(
                                        snapshot[0], msg
                                    )
                                    check()
                                max_id = msg.id
                        if reason:
                            check()
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
                            check()
                except AccountChanged:
                    raise
                except Exception as e:
                    check()
                    logger.warning(
                        "startup_scan_chat_failed",
                        chat_id=chat.id,
                        category=type(e).__name__,
                    )

            check()
            logger.info(
                "startup_scan_completed",
                chats_processed=total,
                reads_queued=queued,
            )
        except AccountChanged:
            raise
        except Exception as e:
            check()
            logger.warning("startup_scan_failed", category=type(e).__name__)

    async def reconcile_autoread(self, adapter: ChatRepository) -> None:
        """Repair missed updates only for explicitly enabled auto-read scopes."""
        raise_if_task_stopped()
        # The recovery task may have yielded between authorizing its saved
        # session and starting this coroutine. Do not adopt a replacement.
        if self.chat_repo is not adapter or not adapter.is_connected():
            return
        generation = self._account_generation

        async def reconcile() -> None:
            try:
                await self._reconcile_autoread(adapter, generation)
            except AccountChanged:
                return

        await run_with_task_deadline(reconcile, None, name="autoread_reconciliation")

    async def _reconcile_autoread(
        self, adapter: ChatRepository, generation: int
    ) -> None:
        def check() -> None:
            self._ensure_account_current(adapter, generation)

        check()
        stored_rules = await self.rule_repo.get_all(rule_type=RuleType.AUTOREAD)
        check()
        rules = [rule for rule in stored_rules if rule.user_id == 1]
        check()
        if not rules or not adapter.is_connected():
            return

        chat_ids = sorted({rule.chat_id for rule in rules})
        check()
        unread_chats = await adapter.get_all_unread_chats(chat_ids)
        check()
        for chat in unread_chats:
            check()
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
                    check()
                    topics = await adapter.get_unread_topics(chat.id, topic_ids)
                    check()
                except FloodWaitError:
                    check()
                    raise
                except AccountChanged:
                    raise
                except Exception as exc:
                    check()
                    logger.warning(
                        "reconcile_forum_failed",
                        chat_id=chat.id,
                        category=type(exc).__name__,
                    )
                    continue
                for topic in topics:
                    check()
                    snapshot = await self._read_snapshot(
                        chat.id, topic.id, check_current=check
                    )
                    if any(rule.rule_type == RuleType.AUTOREAD for rule in snapshot[1]):
                        check()
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
                        check()
            else:
                snapshot = await self._read_snapshot(chat.id, None, check_current=check)
                if any(rule.rule_type == RuleType.AUTOREAD for rule in snapshot[1]):
                    check()
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
                    check()
        check()

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
        guard = (
            self.ai_policy_change(chat_id)
            if rule_type == RuleType.AI_AUTOREAD
            else nullcontext()
        )
        with guard:
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
    ) -> Dict[str, Any]:
        """Evaluate the production decisions without enqueuing Telegram writes."""
        raise_if_task_stopped()
        adapter, generation = self.chat_repo, self._account_generation

        async def simulate() -> Dict[str, Any]:
            return await self._simulate_process_message(
                adapter, generation, chat_id, msg_id, topic_id
            )

        return await run_with_task_deadline(simulate, None, name="dry_run_processing")

    async def _simulate_process_message(
        self,
        adapter: ChatRepository,
        generation: int,
        chat_id: int,
        msg_id: int,
        topic_id: Optional[int],
    ) -> Dict[str, Any]:
        def ensure_current() -> None:
            raise_if_task_stopped()
            if (
                self.chat_repo is not adapter
                or self._account_generation != generation
                or getattr(adapter, "_retired", False)
            ):
                raise AccountChanged()

        ensure_current()
        context = await adapter.get_message_context(chat_id, msg_id)
        ensure_current()
        if not context:
            return {"error": "Message not found"}
        if not context.scope_known:
            return {"error": "Message topic is unknown"}
        if topic_id is not None and topic_id != context.topic_id:
            return {"error": "Message does not belong to this topic"}
        topic_id, msg = context.topic_id, context.message
        snapshot = await self._read_snapshot(
            chat_id, topic_id, check_current=ensure_current
        )
        ensure_current()
        event = SystemEvent(
            type="action" if msg.is_service else "message",
            text=msg.text,
            chat_name="",
            chat_id=chat_id,
            topic_id=topic_id,
            message_model=msg,
        )
        read = await self.get_read_decision(event, msg, snapshot, evaluate_ai=False)
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
            album_key = (chat_id, msg.grouped_id)
            last = self._album_reaction_cache.get(album_key)
            if album_key in self._album_reaction_pending or (
                last is not None and time.monotonic() - last < 60
            ):
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
