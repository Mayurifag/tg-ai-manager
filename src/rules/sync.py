"""Explicitly import a rules export from the configured server."""

import asyncio
import ipaddress
import os
import ssl
from collections.abc import Callable
from contextlib import AbstractContextManager
from urllib.parse import urlsplit

import certifi
import httpx2

from src.infrastructure.json_data import load_json
from src.domain.errors import AccountChanged
from src.infrastructure.logging import get_logger
from src.infrastructure.tasks import (
    raise_if_cancelled,
    raise_if_task_stopped,
    run_with_task_deadline,
)
from src.rules.models import Rule, RuleType
from src.users.ports import UserRepository
from src.requests import AutoreactConfigRequest, SettingsPatch

logger = get_logger(__name__)

_TIMEOUT = 10.0
# shortcut: reject fetch results after 30 seconds; revisit if legitimate
# remote exports routinely take longer. Non-cooperative calls can return later.
_FETCH_TIMEOUT = 30.0
_MAX_IMPORT_BYTES = 2 * 1024 * 1024
_MAX_RULES = 10_000


class RulesImportError(ValueError):
    pass


def _reject_import_constant(_value: str) -> None:
    # json.loads otherwise accepts NaN and infinities, which are not JSON.
    raise ValueError("Invalid import JSON")


async def import_rules_from_remote(
    url: str,
    user_repo: UserRepository,
    *,
    expected_account_id: int | None = None,
    token: str | None = None,
    is_current: Callable[[], bool] | None = None,
    ai_policy_change: Callable[[], AbstractContextManager[None]],
) -> int:
    """Replace local rules only after validating the entire export."""
    raise_if_cancelled()
    if type(expected_account_id) is not int or expected_account_id <= 0:
        raise RulesImportError("Telegram account identity is unavailable.")
    try:
        return await _do_sync(
            url,
            user_repo,
            expected_account_id,
            token,
            is_current,
            ai_policy_change,
        )
    except httpx2.HTTPStatusError as exc:
        logger.warning(
            "rules_sync_failed", category="http_status", status=exc.response.status_code
        )
        raise RulesImportError(
            f"The export server returned HTTP {exc.response.status_code}."
        ) from None
    except AccountChanged, RulesImportError:
        raise
    except ValueError:
        raise RulesImportError(
            "The export is invalid or belongs to another account. No rules changed."
        ) from None
    except Exception as exc:
        # Exception strings may contain request URLs, tokens, or response bodies.
        logger.warning("rules_sync_failed", category=type(exc).__name__)
        raise RulesImportError(
            "Could not confirm the import. Reload and check your rules."
        ) from None
    finally:
        raise_if_cancelled()


def _check_current(is_current: Callable[[], bool] | None) -> None:
    raise_if_cancelled()
    if is_current is not None and not is_current():
        raise AccountChanged()


def _export_tls_context(*, trust_env: bool) -> ssl.SSLContext:
    if trust_env:
        if cafile := os.environ.get("SSL_CERT_FILE"):
            return ssl.create_default_context(cafile=cafile)
        if capath := os.environ.get("SSL_CERT_DIR"):
            return ssl.create_default_context(capath=capath)
    # Keep the export endpoint's existing CA bundle rather than HTTPX2's system default.
    return ssl.create_default_context(cafile=certifi.where())


async def _do_sync(
    url: str,
    user_repo: UserRepository,
    expected_account_id: int,
    token: str | None,
    is_current: Callable[[], bool] | None,
    ai_policy_change: Callable[[], AbstractContextManager[None]],
) -> int:
    # Only explicitly local endpoints may use HTTP or omit a bearer token.
    # An account ID in the export does not authenticate its source.
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if parsed.scheme not in {"http", "https"} or not host:
        raise RulesImportError("The configured export URL is invalid.")
    if not local and (parsed.scheme != "https" or not token or not token.strip()):
        raise RulesImportError("A remote export requires HTTPS and an export token.")
    if (
        token
        and token.strip()
        and (parsed.username is not None or parsed.password is not None)
    ):
        # HTTPX2 gives URL credentials precedence over an Authorization header.
        raise RulesImportError(
            "Remove credentials from the export URL when using a token."
        )
    _check_current(is_current)

    # Bind the import to the account that initiated it, not whichever user
    # happens to exist after a potentially slow network request.
    user = await user_repo.get_user(1)
    if not user or not user.is_authenticated():
        raise AccountChanged()
    _check_current(is_current)
    expected_session = user.session_string

    # Phase: fetch
    headers = {"Accept-Encoding": "identity"}
    if token and token.strip():
        headers["Authorization"] = f"Bearer {token}"
    # Never route the loopback HTTP exception through an environment proxy.
    trust_env = not (local and parsed.scheme == "http")
    deadline = asyncio.get_running_loop().time() + _FETCH_TIMEOUT

    async def fetch() -> bytearray:
        content = bytearray()
        async with httpx2.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=False,
            trust_env=trust_env,
            verify=_export_tls_context(trust_env=trust_env),
        ) as client:
            raise_if_task_stopped()
            _check_current(is_current)
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
                # Enforce the limit before the client can expand a compressed body.
                async for chunk in response.aiter_raw():
                    raise_if_task_stopped()
                    if len(content) + len(chunk) > _MAX_IMPORT_BYTES:
                        raise ValueError("Import is too large")
                    content.extend(chunk)
            raise_if_task_stopped()
        raise_if_task_stopped()
        return content

    content = await run_with_task_deadline(fetch, deadline, name="rules_sync_fetch")

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

    _check_current(is_current)
    with ai_policy_change():
        replaced = await user_repo.replace_rules_and_settings(
            user.id, rules, settings, expected_session=expected_session
        )
    if not replaced:
        raise AccountChanged()

    logger.info(
        "rules_sync_completed",
        rule_count=len(rules),
        settings_synced=bool(settings),
    )
    return len(rules)
