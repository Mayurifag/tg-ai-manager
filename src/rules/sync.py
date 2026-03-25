"""Startup rules sync: fetches production rules export and applies them locally."""

import httpx

from src.infrastructure.logging import get_logger
from src.rules.models import Rule, RuleType
from src.rules.ports import RuleRepository
from src.users.ports import UserRepository

logger = get_logger(__name__)

_TIMEOUT = 10.0

# Fields from the export's user_settings block that map 1-to-1 to User attributes.
_USER_SETTINGS_FIELDS = frozenset(
    {
        "autoread_service_messages",
        "autoread_polls",
        "autoread_self",
        "autoread_bots",
        "autoread_regex",
        "debug_mode",
        "ai_provider",
        "ai_model",
        "ai_api_key",
        "ai_prompt",
    }
)


async def sync_rules_from_remote(
    url: str,
    rule_repo: RuleRepository,
    user_repo: UserRepository,
) -> None:
    """Fetch the production rules export and full-replace local rules.

    Parameters are explicit (no get_settings() call inside) to avoid
    lru_cache issues in tests and to keep this function easily testable.

    On any error the function logs a structured warning and returns —
    startup continues normally (R011).  The full URL is never logged
    because it may contain auth tokens.
    """
    try:
        await _do_sync(url, rule_repo, user_repo)
    except Exception as exc:
        logger.warning("rules_sync_failed", error=str(exc))


async def _do_sync(
    url: str,
    rule_repo: RuleRepository,
    user_repo: UserRepository,
) -> None:
    # Phase: fetch
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(url)
        response.raise_for_status()

    # Phase: parse
    data = response.json()
    remote_rules: list = data.get("rules", [])
    remote_user_settings: dict | None = data.get("user_settings")

    # Phase: apply rules — full replace (delete_all then re-insert)
    await rule_repo.delete_all()
    for raw in remote_rules:
        rule = Rule(
            id=None,
            user_id=1,
            rule_type=RuleType(raw["rule_type"]),
            chat_id=raw["chat_id"],
            topic_id=raw.get("topic_id"),
            # config is already a dict in the export JSON
            config=raw.get("config", {}),
        )
        await rule_repo.add(rule)

    # Phase: apply user settings overlay — only if user row already exists
    settings_synced = False
    if remote_user_settings:
        user = await user_repo.get_user(1)
        if user is not None:
            for field_name, value in remote_user_settings.items():
                if field_name in _USER_SETTINGS_FIELDS and hasattr(user, field_name):
                    current = getattr(user, field_name)
                    # Preserve attribute type: cast booleans explicitly
                    if isinstance(current, bool):
                        setattr(user, field_name, bool(value))
                    else:
                        setattr(user, field_name, value)
            await user_repo.save_user(user)
            settings_synced = True

    logger.info(
        "rules_sync_completed",
        rule_count=len(remote_rules),
        settings_synced=settings_synced,
    )
