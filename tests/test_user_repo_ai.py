"""Tests for User AI fields round-trip via SqliteUserRepository."""

import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest

from src.users.models import User
from src.users.sqlite_repo import SqliteUserRepository


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    session_string TEXT,
    autoread_service_messages INTEGER NOT NULL DEFAULT 0,
    autoread_polls INTEGER NOT NULL DEFAULT 0,
    autoread_self INTEGER NOT NULL DEFAULT 0,
    autoread_bots TEXT,
    autoread_regex TEXT,
    is_premium INTEGER NOT NULL DEFAULT 0,
    debug_mode INTEGER NOT NULL DEFAULT 0,
    ai_provider TEXT,
    ai_model TEXT,
    ai_api_key TEXT,
    ai_prompt TEXT
);
"""

_FAKE_API_HASH = "deadbeefdeadbeefdeadbeefdeadbeef"
_FAKE_SETTINGS_PATCH = {
    "TG_API_ID": 12345,
    "TG_API_HASH": _FAKE_API_HASH,
    "VALKEY_URL": "redis://localhost:6379/0",
    "DB_PATH": ":memory:",
    "WRITE_QUEUE_DELAY": 0.1,
    "RULES_SYNC_URL": None,
}


def _make_repo(db_path: str) -> SqliteUserRepository:
    """Create SqliteUserRepository with patched settings and temp DB."""
    # We must patch both get_settings (used by repo and CryptoManager)
    # and clear the lru_cache so a fresh Settings object is created.
    from src.config import get_settings

    get_settings.cache_clear()

    with patch.dict(
        os.environ,
        {
            "TG_API_ID": str(_FAKE_SETTINGS_PATCH["TG_API_ID"]),
            "TG_API_HASH": _FAKE_API_HASH,
        },
    ):
        get_settings.cache_clear()
        repo = SqliteUserRepository(db_path=db_path)

    return repo


def _create_db(path: str) -> None:
    """Create temp SQLite DB with users schema including AI columns."""
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path():
    """Yield a temp SQLite DB path with users schema applied."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    _create_db(path)
    yield path
    os.unlink(path)


@pytest.fixture()
def repo(db_path):
    """Return a SqliteUserRepository backed by temp DB with patched settings."""
    from src.config import get_settings

    get_settings.cache_clear()
    with patch.dict(
        os.environ,
        {
            "TG_API_ID": str(_FAKE_SETTINGS_PATCH["TG_API_ID"]),
            "TG_API_HASH": _FAKE_API_HASH,
        },
    ):
        get_settings.cache_clear()
        r = SqliteUserRepository(db_path=db_path)
    yield r
    # Restore
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ai_fields_round_trip(repo, db_path):
    """Save User with all four AI fields → get_user → verify all fields match."""
    user = User(
        id=1,
        ai_provider="gemini",
        ai_model="gemini-2.0-flash",
        ai_api_key="super-secret-key",
        ai_prompt="Is this an ad? Reply true/false.",
    )
    await repo.save_user(user)

    result = await repo.get_user(user_id=1)

    assert result is not None
    assert result.ai_provider == "gemini"
    assert result.ai_model == "gemini-2.0-flash"
    assert result.ai_api_key == "super-secret-key"
    assert result.ai_prompt == "Is this an ad? Reply true/false."


@pytest.mark.asyncio
async def test_ai_fields_null_round_trip(repo, db_path):
    """Save User with AI fields as None → get_user → verify fields are None."""
    user = User(
        id=1,
        ai_provider=None,
        ai_model=None,
        ai_api_key=None,
        ai_prompt=None,
    )
    await repo.save_user(user)

    result = await repo.get_user(user_id=1)

    assert result is not None
    assert result.ai_provider is None
    assert result.ai_model is None
    assert result.ai_api_key is None
    assert result.ai_prompt is None


@pytest.mark.asyncio
async def test_ai_api_key_encrypted_at_rest(repo, db_path):
    """Verify ai_api_key stored encrypted (raw DB value ≠ plaintext)."""
    plaintext_key = "my-plaintext-api-key"
    user = User(id=1, ai_api_key=plaintext_key)
    await repo.save_user(user)

    # Read raw DB value bypassing repository
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT ai_api_key FROM users WHERE id = 1").fetchone()
    conn.close()

    raw_value = row[0]
    assert raw_value is not None, "ai_api_key should be stored (not None)"
    assert raw_value != plaintext_key, "ai_api_key must be encrypted at rest"

    # Confirm decrypted value matches plaintext
    result = await repo.get_user(user_id=1)
    assert result.ai_api_key == plaintext_key
