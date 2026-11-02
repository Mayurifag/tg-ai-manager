"""Startup rules sync: fetches production rules export and applies them locally."""

import json
import ipaddress
from urllib.parse import urlsplit

import httpx

from src.infrastructure.logging import get_logger
from src.rules.models import Rule, RuleType
from src.rules.ports import RuleRepository
from src.users.ports import UserRepository
from src.requests import AutoreactConfigRequest, SettingsPatch

logger = get_logger(__name__)

_TIMEOUT = 10.0
_MAX_IMPORT_BYTES = 2 * 1024 * 1024
_MAX_RULES = 10_000


async def sync_rules_from_remote(
    url: str,
    rule_repo: RuleRepository,
    user_repo: UserRepository,
    *,
    expected_account_id: int | None = None,
    token: str | None = None,
) -> None:
    """Fetch the production rules export and full-replace local rules.

    Parameters are explicit (no get_settings() call inside) to avoid
    lru_cache issues in tests and to keep this function easily testable.

    On any error the function logs a structured warning and returns —
    startup continues normally (R011).  The full URL is never logged
    because it may contain auth tokens.
    """
    if type(expected_account_id) is not int or expected_account_id <= 0:
        logger.info("rules_sync_skipped", reason="account_identity_unavailable")
        return
    try:
        await _do_sync(url, rule_repo, user_repo, expected_account_id, token)
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "rules_sync_failed", category="http_status", status=exc.response.status_code
        )
    except httpx.RequestError:
        logger.warning("rules_sync_failed", category="network")
    except Exception as exc:
        # Exception strings may contain request URLs, tokens, or response bodies.
        logger.warning("rules_sync_failed", category=type(exc).__name__)


async def _do_sync(
    url: str,
    rule_repo: RuleRepository,
    user_repo: UserRepository,
    expected_account_id: int,
    token: str | None,
) -> None:
    # Bind the import to the account that initiated it, not whichever user
    # happens to exist after a potentially slow network request.
    user = await user_repo.get_user(1)
    if not user or not user.is_authenticated():
        logger.info("rules_sync_skipped", reason="no_authenticated_account")
        return
    expected_session = user.session_string

    # Phase: fetch
    parsed = urlsplit(url)
    if token and parsed.scheme != "https":
        host = parsed.hostname or ""
        try:
            local = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if parsed.scheme != "http" or not local:
            raise ValueError("Authenticated sync requires HTTPS or loopback")
    content = bytearray()
    headers = {"Accept-Encoding": "identity"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > _MAX_IMPORT_BYTES:
                    raise ValueError("Import is too large")
                content.extend(chunk)

    # Validate the entire document before touching storage.
    data = json.loads(content)
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise ValueError("Import must contain a rules list")
    if (
        type(data.get("account_id")) is not int
        or data["account_id"] != expected_account_id
    ):
        raise ValueError("Import account does not match the active account")
    remote_rules = data["rules"]
    if len(remote_rules) > _MAX_RULES:
        raise ValueError("Too many rules")
    remote_settings = data.get("user_settings", {})
    if not isinstance(remote_settings, dict) or "ai_api_key" in remote_settings:
        raise ValueError("Invalid import settings")
    settings = (
        SettingsPatch.from_json(remote_settings).values if remote_settings else {}
    )

    rules = []
    scopes = set()
    for raw in remote_rules:
        if not isinstance(raw, dict) or not raw.keys() <= {
            "rule_type",
            "chat_id",
            "topic_id",
            "config",
        }:
            raise ValueError("Invalid rule")
        chat_id, topic_id = raw.get("chat_id"), raw.get("topic_id")
        if type(chat_id) is not int or chat_id == 0 or not -(2**63) <= chat_id < 2**63:
            raise ValueError("Invalid chat ID")
        if topic_id is not None and (
            type(topic_id) is not int or not 0 < topic_id < 2**31
        ):
            raise ValueError("Invalid topic ID")
        rule_type = RuleType(raw["rule_type"])
        config = raw.get("config", {})
        if not isinstance(config, dict):
            raise ValueError("Invalid rule config")
        if rule_type == RuleType.AUTOREACT:
            AutoreactConfigRequest.from_json(
                {
                    "chat_id": chat_id,
                    "topic_id": topic_id,
                    "enabled": True,
                    "config": config,
                }
            )
        elif config:
            raise ValueError("Invalid rule config")
        scope = (chat_id, topic_id, rule_type)
        if scope in scopes:
            raise ValueError("Duplicate rule scope")
        scopes.add(scope)
        rules.append(
            Rule(
                user_id=1,
                rule_type=rule_type,
                chat_id=chat_id,
                topic_id=topic_id,
                config=config,
            )
        )

    replaced = await user_repo.replace_rules_and_settings(
        user.id, rules, settings, expected_session=expected_session
    )
    if not replaced:
        logger.warning("rules_sync_skipped", reason="account_changed")
        return

    logger.info(
        "rules_sync_completed",
        rule_count=len(rules),
        settings_synced=bool(settings),
    )
