import traceback
from datetime import datetime

from src.domain.models import SystemEvent, ActionLog
from src.ai.factory import create_ai_provider
from src.ai.classifiers.ad_classifier import AdClassifier
from src.settings.sqlite_repo import SqliteSettingsRepository
from src.domain.ports import ActionRepository
from src.infrastructure.queue_service import QueueService
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class SkipAdsHandler:
    def __init__(
        self,
        settings_repo: SqliteSettingsRepository,
        action_repo: ActionRepository,
        queue_service: QueueService,
    ):
        self.settings_repo = settings_repo
        self.action_repo = action_repo
        self.queue_service = queue_service
        self._provider = None
        self._classifier = None

    async def _get_classifier(self):
        # Re-create provider if settings changed? For MVP we create once or fetch fresh settings.
        # Fetching fresh settings every time is safer for config changes.
        settings = await self.settings_repo.get_settings()
        if not settings.ai_enabled or not settings.skip_ads_enabled:
            return None

        provider = create_ai_provider(settings)
        if not provider:
            return None

        return AdClassifier(provider)

    async def handle(self, event: SystemEvent):
        if event.type != "message" or not event.message_model:
            return

        if event.is_read:  # Already read by other rules
            return

        try:
            classifier = await self._get_classifier()
            if not classifier:
                return

            msg = event.message_model
            # Only classify incoming messages from channels or groups (skip DMs to be safe?)
            # Or skip bots?
            if msg.is_outgoing:
                return

            is_ad = await classifier.is_ad(msg.text)

            if is_ad:
                await self.queue_service.enqueue_mark_read(
                    event.chat_id, event.topic_id, max_id=msg.id
                )
                event.is_read = True

                await self.action_repo.add_log(
                    ActionLog(
                        action="skip_ads_read",
                        chat_id=event.chat_id,
                        chat_name=event.chat_name,
                        reason="ai_classified_ad",
                        date=datetime.now(),
                        link=event.link,
                    )
                )
                logger.info("ad_skipped", chat_id=event.chat_id)

        except Exception as e:
            logger.error(
                "skip_ads_handler_error", error=str(e), traceback=traceback.format_exc()
            )
