"""Startup rules sync: fetches production rules export and applies them locally."""

import asyncio
import ipaddress
from collections.abc import Callable
from contextlib import AbstractContextManager
from urllib.parse import urlsplit

import httpx

from src.infrastructure.json_data import load_json
from src.infrastructure.logging import get_logger
from src.infrastructure.tasks import (
    raise_if_cancelled,
    raise_if_task_stopped,
    run_with_task_deadline,
)
from src.rules.models import Rule, RuleType
from src.rules.ports import RuleRepository
from src.users.ports import UserRepository
from src.requests import AutoreactConfigRequest, SettingsPatch

logger = get_logger(__name__)

_TIMEOUT = 10.0
# shortcut: reject fetch results after 30 seconds; revisit if legitimate
# remote exports routinely take longer. Non-cooperative calls can return later.
_FETCH_TIMEOUT = 30.0
_MAX_IMPORT_BYTES = 2 * 1024 * 1024
_MAX_RULES = 10_000


def _reject_import_constant(_value: str) -> None:
    # json.loads otherwise accepts NaN and infinities, which are not JSON.
    raise ValueError("Invalid import JSON")


async def sync_rules_from_remote(
    url: str,
    rule_repo: RuleRepository,
    user_repo: UserRepository,
    *,
    expected_account_id: int | None = None,
    token: str | None = None,
    is_current: Callable[[], bool] | None = None,
    ai_policy_change: Callable[[], AbstractContextManager[None]],
) -> None:
    """Fetch the production rules export and full-replace local rules.

    Parameters are explicit rather than read from cached global settings.
    The policy guard must belong to the RuleService using these repositories.

    On any error the function logs a structured warning and returns —
    startup continues normally (R011).  The full URL is never logged
    because it may contain auth tokens.
    """
    raise_if_cancelled()
    if type(expected_account_id) is not int or expected_account_id <= 0:
        logger.info("rules_sync_skipped", reason="account_identity_unavailable")
        return
    try:
        await _do_sync(
            url,
            rule_repo,
            user_repo,
            expected_account_id,
            token,
            is_current,
            ai_policy_change,
        )
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "rules_sync_failed", category="http_status", status=exc.response.status_code
        )
    except httpx.RequestError:
        logger.warning("rules_sync_failed", category="network")
    except TimeoutError:
        logger.warning("rules_sync_failed", category="timeout")
    except Exception as exc:
        # Exception strings may contain request URLs, tokens, or response bodies.
        logger.warning("rules_sync_failed", category=type(exc).__name__)
    finally:
        raise_if_cancelled()


def _sync_is_current(is_current: Callable[[], bool] | None) -> bool:
    raise_if_cancelled()
    if is_current is not None and not is_current():
        logger.info("rules_sync_skipped", reason="account_changed")
        return False
    return True


async def _do_sync(
    url: str,
    rule_repo: RuleRepository,
    user_repo: UserRepository,
    expected_account_id: int,
    token: str | None,
    is_current: Callable[[], bool] | None,
    ai_policy_change: Callable[[], AbstractContextManager[None]],
) -> None:
    # Only explicitly local endpoints may use HTTP or omit a bearer token.
    # An account ID in the export does not authenticate its source.
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if parsed.scheme not in {"http", "https"} or not host:
        logger.warning("rules_sync_skipped", reason="invalid_url")
        return
    if not local and (parsed.scheme != "https" or not token or not token.strip()):
        logger.warning("rules_sync_skipped", reason="remote_requires_https_and_token")
        return
    if (
        token
        and token.strip()
        and (parsed.username is not None or parsed.password is not None)
    ):
        # HTTPX gives URL credentials precedence over an Authorization header.
        logger.warning("rules_sync_skipped", reason="url_credentials_override_token")
        return
    if not _sync_is_current(is_current):
        return

    # Bind the import to the account that initiated it, not whichever user
    # happens to exist after a potentially slow network request.
    user = await user_repo.get_user(1)
    if not user or not user.is_authenticated():
        logger.info("rules_sync_skipped", reason="no_authenticated_account")
        return
    if not _sync_is_current(is_current):
        return
    expected_session = user.session_string

    # Phase: fetch
    headers = {"Accept-Encoding": "identity"}
    if token and token.strip():
        headers["Authorization"] = f"Bearer {token}"
    # Never route the loopback HTTP exception through an environment proxy.
    deadline = asyncio.get_running_loop().time() + _FETCH_TIMEOUT

    async def fetch() -> bytearray | None:
        content = bytearray()
        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=False,
            trust_env=not (local and parsed.scheme == "http"),
        ) as client:
            raise_if_task_stopped()
            if not _sync_is_current(is_current):
                return None
            raise_if_task_stopped()
            async with client.stream("GET", url, headers=headers) as response:
                raise_if_task_stopped()
                response.raise_for_status()
                raise_if_task_stopped()
                if response.headers.get("Content-Encoding", "").strip().lower() not in (
                    "",
                    "identity",
                ):
                    raise ValueError("Encoded imports are not supported")
                # Enforce the limit before HTTPX can expand a compressed body.
                async for chunk in response.aiter_raw():
                    raise_if_task_stopped()
                    if len(content) + len(chunk) > _MAX_IMPORT_BYTES:
                        raise ValueError("Import is too large")
                    content.extend(chunk)
            raise_if_task_stopped()
        raise_if_task_stopped()
        return content

    content = await run_with_task_deadline(fetch, deadline, name="rules_sync_fetch")
    if content is None:
        return

    # Validate the entire document before touching storage.
    data = load_json(content, parse_constant=_reject_import_constant)
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

    if not _sync_is_current(is_current):
        return
    with ai_policy_change():
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
