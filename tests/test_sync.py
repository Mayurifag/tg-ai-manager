"""Tests for sync_rules_from_remote() — startup rules sync (S02).

Covers:
  R010 — Happy path: fetch JSON, delete all local rules, insert remote rules,
          overlay user settings.
  R011 — Failure modes: HTTP error, bad JSON, timeout all log a warning and
          don't raise; startup continues.
  R012 — Full replace: delete_all() is called before any add().

Technique: httpx.AsyncClient is mocked via unittest.mock.patch on the module
that imports it (src.rules.sync).  Rule/User repos are AsyncMock instances
passed as explicit parameters, so no Quart app is needed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src.rules.models import Rule, RuleType
from src.rules.sync import sync_rules_from_remote
from src.users.models import User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXPORT_RULES = [
    {
        "rule_type": "autoread",
        "chat_id": 111,
        "topic_id": None,
        "config": {},
    },
    {
        "rule_type": "autoreact",
        "chat_id": 222,
        "topic_id": 5,
        "config": {"emoji": "👍"},
    },
]

_EXPORT_USER_SETTINGS = {
    "autoread_service_messages": True,
    "autoread_polls": True,
    "autoread_self": False,
    "autoread_bots": "@mybot",
    "autoread_regex": "hello",
    "debug_mode": True,
    "ai_provider": "openai",
    "ai_model": "gpt-4o",
    "ai_api_key": "sk-test-key",
    "ai_prompt": "custom prompt",
}

_EXPORT_PAYLOAD = {
    "rules": _EXPORT_RULES,
    "user_settings": _EXPORT_USER_SETTINGS,
}

_URL = "https://prod.example.com/api/rules/export?token=secret"


def _make_mock_response(payload: dict, status_code: int = 200) -> MagicMock:
    """Return a fake httpx response whose .json() returns *payload*."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()  # no-op by default
    return resp


def _make_repos(user: User | None = None):
    """Return (rule_repo, user_repo) AsyncMock pair."""
    rule_repo = AsyncMock()
    rule_repo.delete_all = AsyncMock(return_value=None)
    rule_repo.add = AsyncMock(return_value=1)

    user_repo = AsyncMock()
    user_repo.get_user = AsyncMock(return_value=user)
    user_repo.save_user = AsyncMock(return_value=None)

    return rule_repo, user_repo


# ---------------------------------------------------------------------------
# Test 1 — Happy path: 2 rules + user settings
# ---------------------------------------------------------------------------


async def test_happy_path_two_rules_and_user_settings():
    """R010 + R012: delete_all called first, add called twice with correct Rule
    objects, user settings overlaid via save_user."""
    user = User(
        api_id=12345,
        api_hash="abc",
        session_string="sess",
        autoread_service_messages=False,
        autoread_polls=False,
        autoread_self=False,
        autoread_bots="@old_bot",
        autoread_regex="",
        debug_mode=False,
    )
    rule_repo, user_repo = _make_repos(user=user)
    mock_response = _make_mock_response(_EXPORT_PAYLOAD)

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=False)

    with patch("src.rules.sync.httpx.AsyncClient", return_value=mock_client_instance):
        await sync_rules_from_remote(_URL, rule_repo, user_repo)

    # delete_all must have been called exactly once
    rule_repo.delete_all.assert_called_once()

    # add must have been called twice, in order
    assert rule_repo.add.call_count == 2

    # Verify call order: delete_all before any add
    manager = MagicMock()
    manager.attach_mock(rule_repo.delete_all, "delete_all")
    manager.attach_mock(rule_repo.add, "add")
    # Simpler: verify delete_all was called before add via position in mock_calls
    all_calls = rule_repo.mock_calls
    delete_idx = next(i for i, c in enumerate(all_calls) if c[0] == "delete_all")
    add_idx = next(i for i, c in enumerate(all_calls) if c[0] == "add")
    assert delete_idx < add_idx, "delete_all must be called before add()"

    # Inspect the Rule objects passed to add()
    first_call_args = rule_repo.add.call_args_list[0][0][0]
    assert isinstance(first_call_args, Rule)
    assert first_call_args.id is None
    assert first_call_args.user_id == 1
    assert first_call_args.rule_type == RuleType.AUTOREAD
    assert first_call_args.chat_id == 111
    assert first_call_args.topic_id is None
    assert first_call_args.config == {}

    second_call_args = rule_repo.add.call_args_list[1][0][0]
    assert isinstance(second_call_args, Rule)
    assert second_call_args.id is None
    assert second_call_args.user_id == 1
    assert second_call_args.rule_type == RuleType.AUTOREACT
    assert second_call_args.chat_id == 222
    assert second_call_args.topic_id == 5
    assert second_call_args.config == {"emoji": "👍"}

    # get_user(1) must have been called
    user_repo.get_user.assert_called_once_with(1)

    # save_user must have been called once with the mutated user
    user_repo.save_user.assert_called_once()
    saved_user = user_repo.save_user.call_args[0][0]
    # Sensitive fields untouched
    assert saved_user.api_id == 12345
    assert saved_user.api_hash == "abc"
    assert saved_user.session_string == "sess"
    # Settings fields overlaid from export
    assert saved_user.autoread_service_messages is True
    assert saved_user.autoread_polls is True
    assert saved_user.autoread_self is False
    assert saved_user.autoread_bots == "@mybot"
    assert saved_user.autoread_regex == "hello"
    assert saved_user.debug_mode is True


# ---------------------------------------------------------------------------
# Test 2 — Empty rules list
# ---------------------------------------------------------------------------


async def test_empty_rules_list_no_add_calls():
    """R010 + R012: rules=[] → delete_all called, no add calls, user settings
    still updated."""
    payload = {"rules": [], "user_settings": _EXPORT_USER_SETTINGS}
    user = User()
    rule_repo, user_repo = _make_repos(user=user)
    mock_response = _make_mock_response(payload)

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=False)

    with patch("src.rules.sync.httpx.AsyncClient", return_value=mock_client_instance):
        await sync_rules_from_remote(_URL, rule_repo, user_repo)

    rule_repo.delete_all.assert_called_once()
    rule_repo.add.assert_not_called()
    user_repo.save_user.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3 — No existing user row
# ---------------------------------------------------------------------------


async def test_no_user_row_rules_synced_save_user_not_called():
    """R010: get_user returns None → rules are synced but save_user never
    called — no ghost row created."""
    rule_repo, user_repo = _make_repos(user=None)  # no user row
    mock_response = _make_mock_response(_EXPORT_PAYLOAD)

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=False)

    with patch("src.rules.sync.httpx.AsyncClient", return_value=mock_client_instance):
        await sync_rules_from_remote(_URL, rule_repo, user_repo)

    # Rules must still be synced
    rule_repo.delete_all.assert_called_once()
    assert rule_repo.add.call_count == 2

    # But no ghost user should be created
    user_repo.save_user.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4 — HTTP error
# ---------------------------------------------------------------------------


async def test_http_error_logs_warning_no_repo_calls(caplog):
    """R011: httpx.HTTPStatusError → warning logged, no repo calls at all."""
    rule_repo, user_repo = _make_repos()

    request = MagicMock()
    response_obj = MagicMock()
    response_obj.status_code = 403

    http_error = httpx.HTTPStatusError(
        "403 Forbidden", request=request, response=response_obj
    )

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=MagicMock())
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=False)

    # raise_for_status raises the HTTP error
    mock_client_instance.get.return_value.raise_for_status = MagicMock(
        side_effect=http_error
    )

    with patch("src.rules.sync.httpx.AsyncClient", return_value=mock_client_instance):
        # Must NOT raise — failure is non-fatal
        await sync_rules_from_remote(_URL, rule_repo, user_repo)

    rule_repo.delete_all.assert_not_called()
    rule_repo.add.assert_not_called()
    user_repo.get_user.assert_not_called()
    user_repo.save_user.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5 — Bad JSON (missing "rules" key handled gracefully, but missing
# "rules" key returns [] via .get, so test a truly broken payload: response
# where .json() raises an exception)
# ---------------------------------------------------------------------------


async def test_bad_json_no_rules_key_no_delete_all_called():
    """R011: response.json() raises ValueError → warning logged, delete_all
    never called (data was never parsed cleanly)."""
    rule_repo, user_repo = _make_repos()

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()  # HTTP 200, no raise
    mock_response.json.side_effect = ValueError("Expecting value: line 1 column 1")

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=False)

    with patch("src.rules.sync.httpx.AsyncClient", return_value=mock_client_instance):
        await sync_rules_from_remote(_URL, rule_repo, user_repo)

    rule_repo.delete_all.assert_not_called()
    rule_repo.add.assert_not_called()
    user_repo.save_user.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5b — Payload missing "rules" key (uses .get default of [])
# ---------------------------------------------------------------------------


async def test_missing_rules_key_in_payload_no_add_delete_still_called():
    """R010 + R012: if payload has no 'rules' key, .get('rules', []) returns
    [] — delete_all IS called (full replace) but add is not."""
    payload = {"user_settings": _EXPORT_USER_SETTINGS}  # no "rules" key
    user = User()
    rule_repo, user_repo = _make_repos(user=user)
    mock_response = _make_mock_response(payload)

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=False)

    with patch("src.rules.sync.httpx.AsyncClient", return_value=mock_client_instance):
        await sync_rules_from_remote(_URL, rule_repo, user_repo)

    # delete_all called (full replace semantics even for empty remote set)
    rule_repo.delete_all.assert_called_once()
    rule_repo.add.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6 — Network timeout
# ---------------------------------------------------------------------------


async def test_network_timeout_logs_warning_no_repo_calls():
    """R011: httpx.ConnectTimeout → warning logged, no repo calls at all."""
    rule_repo, user_repo = _make_repos()

    timeout_error = httpx.ConnectTimeout("timed out")

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(side_effect=timeout_error)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=False)

    with patch("src.rules.sync.httpx.AsyncClient", return_value=mock_client_instance):
        await sync_rules_from_remote(_URL, rule_repo, user_repo)

    rule_repo.delete_all.assert_not_called()
    rule_repo.add.assert_not_called()
    user_repo.get_user.assert_not_called()
    user_repo.save_user.assert_not_called()


# ---------------------------------------------------------------------------
# Test 7 — URL never appears in logs (token safety)
# ---------------------------------------------------------------------------


async def test_url_not_logged_on_failure(caplog):
    """R011: the URL (which may contain auth tokens) must not appear in any
    log output on failure."""
    rule_repo, user_repo = _make_repos()
    secret_url = "https://prod.example.com/export?token=SUPER_SECRET_TOKEN"

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=False)

    import io
    import logging

    # Capture stdlib logging output (structlog forwards to stdlib in test mode)
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    logging.root.addHandler(handler)
    try:
        with patch(
            "src.rules.sync.httpx.AsyncClient", return_value=mock_client_instance
        ):
            await sync_rules_from_remote(secret_url, rule_repo, user_repo)
    finally:
        logging.root.removeHandler(handler)

    log_output = log_stream.getvalue()
    assert "SUPER_SECRET_TOKEN" not in log_output, (
        "Secret token must not appear in log output"
    )
    assert secret_url not in log_output, "Full URL must not appear in log output"


# ---------------------------------------------------------------------------
# Test 8 — User settings only updates known safe fields
# ---------------------------------------------------------------------------


async def test_user_settings_only_updates_allowlisted_fields():
    """R010: extra/unknown keys in user_settings are silently ignored — no
    arbitrary attribute injection onto the User object."""
    malicious_payload = {
        "rules": [],
        "user_settings": {
            "autoread_service_messages": True,
            "debug_mode": True,
            # injected unknown field — must be ignored
            "api_hash": "INJECTED",
            "session_string": "STOLEN",
        },
    }
    user = User(api_id=999, api_hash="original_hash", session_string="original_sess")
    rule_repo, user_repo = _make_repos(user=user)
    mock_response = _make_mock_response(malicious_payload)

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=False)

    with patch("src.rules.sync.httpx.AsyncClient", return_value=mock_client_instance):
        await sync_rules_from_remote(_URL, rule_repo, user_repo)

    saved_user = user_repo.save_user.call_args[0][0]
    # Sensitive fields must NOT have been overwritten
    assert saved_user.api_hash == "original_hash"
    assert saved_user.session_string == "original_sess"
    # Allowlisted fields ARE updated
    assert saved_user.autoread_service_messages is True
    assert saved_user.debug_mode is True


# ---------------------------------------------------------------------------
# Test 9 — AI fields synced from export
# ---------------------------------------------------------------------------


async def test_ai_fields_synced_from_export():
    """R032: ai_provider, ai_model, ai_api_key, ai_prompt are in
    _USER_SETTINGS_FIELDS and are overlaid on the local user during sync."""
    payload = {
        "rules": [],
        "user_settings": {
            "autoread_service_messages": False,
            "autoread_polls": False,
            "autoread_self": False,
            "autoread_bots": "@bot",
            "autoread_regex": "",
            "debug_mode": False,
            "ai_provider": "openai",
            "ai_model": "gpt-4o",
            "ai_api_key": "sk-synced-key",
            "ai_prompt": "synced prompt",
        },
    }
    user = User(
        ai_provider=None,
        ai_model=None,
        ai_api_key=None,
        ai_prompt=None,
    )
    rule_repo, user_repo = _make_repos(user=user)
    mock_response = _make_mock_response(payload)

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=False)

    with patch("src.rules.sync.httpx.AsyncClient", return_value=mock_client_instance):
        await sync_rules_from_remote(_URL, rule_repo, user_repo)

    user_repo.save_user.assert_called_once()
    saved_user = user_repo.save_user.call_args[0][0]

    # AI fields must be overlaid from the remote export
    assert saved_user.ai_provider == "openai"
    assert saved_user.ai_model == "gpt-4o"
    assert saved_user.ai_api_key == "sk-synced-key"
    assert saved_user.ai_prompt == "synced prompt"
