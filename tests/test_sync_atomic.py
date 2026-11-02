import asyncio
import json
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from alembic import command
from alembic.config import Config
from quart import Quart
from telethon import types

from src.config import get_settings
from src.adapters.telegram.chat_query_ops import ChatQueryOps
from src.infrastructure.maintenance import job_background_maintenance
from src.rules.models import Rule, RuleType
from src.rules.sqlite_repo import SqliteRuleRepository
from src.rules.sync import sync_rules_from_remote
from src.users.models import User
from src.users.sqlite_repo import SqliteUserRepository
from src.web.routes import register_routes


@pytest.fixture
def repositories(tmp_path, monkeypatch):
    path = str(tmp_path / "migrated.db")
    monkeypatch.setenv("DB_PATH", path)
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "synthetic-key")
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    yield SqliteRuleRepository(path), SqliteUserRepository(path)
    get_settings.cache_clear()


async def test_populated_legacy_duplicate_rules_keep_oldest_scope_on_upgrade(
    tmp_path, monkeypatch
):
    path = str(tmp_path / "legacy.db")
    backup = str(tmp_path / "before-upgrade.db")
    monkeypatch.setenv("DB_PATH", path)
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "synthetic-migration-key")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "005_add_ai_fields")
        users = SqliteUserRepository(path)
        rules = SqliteRuleRepository(path)
        await users.save_user(
            User(
                username="synthetic",
                session_string="synthetic-session",
                ai_api_key="synthetic-ai-key",
            )
        )
        for topic, emoji in ((None, "💩"), (None, "👍"), (7, "❤️"), (7, "🤡")):
            await rules.add(
                Rule(
                    chat_id=-100123,
                    topic_id=topic,
                    rule_type=RuleType.AUTOREACT,
                    config={"emoji": emoji, "target_users": [123]},
                )
            )
        with sqlite3.connect(path) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)

        command.upgrade(Config("alembic.ini"), "head")
        migrated = await rules.get_all()
        assert [(rule.topic_id, rule.config["emoji"]) for rule in migrated] == [
            (None, "💩"),
            (7, "❤️"),
        ]
        user = await users.get_user(1)
        assert (
            user.username == "synthetic" and user.session_string == "synthetic-session"
        )
        assert user.ai_api_key == "synthetic-ai-key"
        with sqlite3.connect(backup) as conn:
            assert conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 4
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        with pytest.raises(sqlite3.IntegrityError):
            await rules.add(
                Rule(chat_id=-100123, topic_id=7, rule_type=RuleType.AUTOREACT)
            )
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("failure", ["offline", "empty", "empty_user"])
async def test_failed_premium_lookup_does_not_overwrite_saved_status(
    repositories, failure
):
    _, users = repositories
    await users.save_user(User(session_string="synthetic-session", is_premium=True))
    shutdown = asyncio.Event()

    async def get_self(_request):
        shutdown.set()
        if failure == "empty":
            return []
        if failure == "empty_user":
            return [types.UserEmpty(id=1)]
        raise OSError("synthetic offline")

    ops = ChatQueryOps(get_self, None, None)
    interactor = SimpleNamespace(
        run_storage_maintenance=AsyncMock(),
        get_self_premium_status=ops.get_self_premium_status,
    )
    await job_background_maintenance(
        AsyncMock(), AsyncMock(), interactor, users, shutdown
    )

    assert (await users.get_user(1)).is_premium is True


@pytest.mark.parametrize("premium", [True, False, None])
async def test_successful_premium_lookup_updates_saved_status(repositories, premium):
    _, users = repositories
    await users.save_user(User(session_string="synthetic-session", is_premium=True))
    shutdown = asyncio.Event()

    async def get_self(_request):
        shutdown.set()
        return [types.User(id=1, premium=premium)]

    ops = ChatQueryOps(get_self, None, None)
    interactor = SimpleNamespace(
        run_storage_maintenance=AsyncMock(),
        get_self_premium_status=ops.get_self_premium_status,
    )
    await job_background_maintenance(
        AsyncMock(), AsyncMock(), interactor, users, shutdown
    )
    assert (await users.get_user(1)).is_premium is bool(premium)


async def sync_payload(payload, rule_repo, user_repo, expected_account_id=42):
    payload = {"account_id": 42, **payload}
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    with patch("src.rules.sync.httpx.AsyncClient", return_value=client):
        await sync_rules_from_remote(
            "https://example.test/export",
            rule_repo,
            user_repo,
            expected_account_id=expected_account_id,
        )


async def seed(rule_repo, user_repo):
    await user_repo.save_user(
        User(session_string="synthetic-session", debug_mode=False)
    )
    await rule_repo.add(Rule(chat_id=123, rule_type=RuleType.AUTOREAD))


@pytest.mark.parametrize(
    "payload",
    [
        {"user_settings": {"debug_mode": True}},
        {
            "rules": [
                {"chat_id": 456, "rule_type": "autoread"},
                {"chat_id": 789, "rule_type": "invalid"},
            ]
        },
        {
            "rules": [
                {"chat_id": 456, "rule_type": "autoread"},
                {"chat_id": 456, "rule_type": "autoread"},
            ]
        },
        {"rules": [], "user_settings": {"debug_mode": "false"}},
        {"rules": [{"chat_id": True, "rule_type": "autoread"}]},
        {
            "rules": [
                {"chat_id": 456, "rule_type": "autoreact", "config": {"emoji": "👍"}}
            ]
        },
    ],
)
async def test_invalid_import_preserves_existing_rules_and_settings(
    repositories, payload
):
    rules, users = repositories
    await seed(rules, users)
    await sync_payload(payload, rules, users)
    assert [r.chat_id for r in await rules.get_all()] == [123]
    assert not (await users.get_user()).debug_mode


async def test_import_requires_existing_account(repositories):
    rules, users = repositories
    await sync_payload(
        {"rules": [{"chat_id": 456, "rule_type": "autoread"}]}, rules, users
    )
    assert await users.get_user() is None
    assert await rules.get_all() == []


async def test_import_rejects_a_different_account(repositories):
    rules, users = repositories
    await seed(rules, users)
    await sync_payload({"rules": []}, rules, users, expected_account_id=99)
    assert [r.chat_id for r in await rules.get_all()] == [123]


async def test_explicit_empty_import_replaces_rules_and_updates_settings(repositories):
    rules, users = repositories
    await seed(rules, users)
    await sync_payload(
        {"rules": [], "user_settings": {"debug_mode": True}}, rules, users
    )
    assert await rules.get_all() == []
    assert (await users.get_user()).debug_mode


async def test_sqlite_failure_rolls_back_both_rules_and_settings(repositories):
    rules, users = repositories
    await seed(rules, users)
    with rules._connect() as conn:
        conn.execute(
            "CREATE TRIGGER fail_import BEFORE INSERT ON rules WHEN NEW.chat_id = 456 "
            "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
    await sync_payload(
        {
            "rules": [{"chat_id": 456, "rule_type": "autoread"}],
            "user_settings": {"debug_mode": True},
        },
        rules,
        users,
    )
    assert [r.chat_id for r in await rules.get_all()] == [123]
    assert not (await users.get_user()).debug_mode


async def test_cancelled_import_rolls_back_before_returning(repositories, monkeypatch):
    rules, users = repositories
    await seed(rules, users)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    def delayed_json(value):
        loop.call_soon_threadsafe(started.set)
        if not release.wait(5):
            raise TimeoutError("synthetic import timed out")
        return json.dumps(value)

    monkeypatch.setattr(
        "src.users.sqlite_repo.json", SimpleNamespace(dumps=delayed_json)
    )
    task = asyncio.create_task(
        users.replace_rules_and_settings(
            1,
            [Rule(chat_id=456)],
            {"debug_mode": True},
            expected_session="synthetic-session",
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert [rule.chat_id for rule in await rules.get_all()] == [123]
    assert not (await users.get_user()).debug_mode


async def test_valid_import_preserves_local_credentials(repositories):
    rules, users = repositories
    await users.save_user(
        User(session_string="session", ai_api_key="local-key", is_premium=True)
    )
    await rules.add(Rule(chat_id=123))
    await sync_payload(
        {
            "rules": [
                {
                    "chat_id": -456,
                    "rule_type": "autoreact",
                    "config": {"emoji": "👍", "target_users": []},
                }
            ],
            "user_settings": {"debug_mode": True},
        },
        rules,
        users,
    )
    imported = await rules.get_all()
    user = await users.get_user()
    assert [(rule.chat_id, rule.rule_type) for rule in imported] == [
        (-456, RuleType.AUTOREACT)
    ]
    assert user.debug_mode and user.is_premium
    assert user.ai_api_key == "local-key" and user.session_string == "session"


async def test_exported_legacy_reaction_rules_sync_with_existing_defaults(
    repositories, tmp_path
):
    dest_rules, dest_users = repositories
    source_path = str(tmp_path / "source.db")
    with (
        closing(sqlite3.connect(dest_rules.db_path)) as source,
        closing(sqlite3.connect(source_path)) as dest,
    ):
        source.backup(dest)
    source_rules = SqliteRuleRepository(source_path)
    source_users = SqliteUserRepository(source_path)
    await source_users.save_user(User(session_string="source-session"))
    await dest_users.save_user(User(session_string="destination-session"))

    configs = [
        {"emoji": "👍"},
        {},
        {"emoji": "❤️", "target_users": None},
        {"emoji": "👍", "target_users": [55]},
    ]
    for chat_id, config in enumerate(configs, start=100):
        await source_rules.add(
            Rule(chat_id=chat_id, rule_type=RuleType.AUTOREACT, config=config)
        )

    app = Quart(__name__)
    app.login_lock = asyncio.Lock()
    app.user_repo = source_users
    app.tg_adapter = SimpleNamespace(_self_id=42, is_connected=lambda: True)
    register_routes(app)
    token = "synthetic-export-token-0123456789abcdef"
    with (
        patch(
            "src.web.routes.settings.SqliteRuleRepository", return_value=source_rules
        ),
        patch(
            "src.web.routes.settings.get_settings",
            return_value=SimpleNamespace(DB_PATH=source_path, RULES_EXPORT_TOKEN=token),
        ),
    ):
        response = await app.test_client().get(
            "/api/rules/export", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 200
    payload = await response.get_json()
    expected = [
        {"emoji": "👍", "target_users": []},
        {"emoji": "💩", "target_users": []},
        {"emoji": "❤️", "target_users": []},
        {"emoji": "👍", "target_users": [55]},
    ]
    assert [rule["config"] for rule in payload["rules"]] == expected
    assert [rule.config for rule in await source_rules.get_all()] == configs

    await sync_payload(payload, dest_rules, dest_users)
    imported = await dest_rules.get_all()
    assert [rule.config for rule in imported] == expected


async def test_import_does_not_modify_replaced_account(repositories):
    rules, users = repositories
    await seed(rules, users)
    await users.delete_user(1)
    await users.save_user(User(session_string="new-session"))
    assert not await users.replace_rules_and_settings(
        1,
        [Rule(chat_id=456)],
        {"debug_mode": True},
        expected_session="synthetic-session",
    )
    assert await rules.get_all() == []
    assert not (await users.get_user()).debug_mode


async def test_account_replaced_during_fetch_cannot_receive_old_account_rules(
    repositories,
):
    rules, users = repositories
    await seed(rules, users)
    started, release = asyncio.Event(), asyncio.Event()

    async def transport(request):
        started.set()
        await release.wait()
        return httpx.Response(
            200,
            json={
                "account_id": 42,
                "rules": [{"chat_id": 456, "rule_type": "autoread"}],
                "user_settings": {"debug_mode": True},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    with patch("src.rules.sync.httpx.AsyncClient", return_value=client):
        task = asyncio.create_task(
            sync_rules_from_remote(
                "https://example.test/export", rules, users, expected_account_id=42
            )
        )
        await asyncio.wait_for(started.wait(), 1)
        await users.delete_user(1)
        await users.save_user(User(session_string="replacement-session"))
        await rules.add(Rule(chat_id=789))
        release.set()
        await asyncio.wait_for(task, 1)

    assert [rule.chat_id for rule in await rules.get_all()] == [789]
    assert not (await users.get_user()).debug_mode
