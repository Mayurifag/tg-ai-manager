"""Network and input failures must not mutate the local account."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.rules.sync import _MAX_IMPORT_BYTES, sync_rules_from_remote
from src.users.models import User


@pytest.mark.parametrize("failure", ["status", "timeout", "json", "oversize"])
async def test_failed_remote_sync_never_writes(failure):
    requests = []

    def response(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ConnectTimeout("synthetic timeout", request=request)
        if failure == "status":
            return httpx.Response(403)
        if failure == "json":
            return httpx.Response(200, content=b"not json")
        return httpx.Response(200, content=b"x" * (_MAX_IMPORT_BYTES + 1))

    client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    rules = AsyncMock()
    users = AsyncMock()
    users.get_user.return_value = User(
        api_id=1, api_hash="synthetic", session_string="synthetic-session"
    )
    with patch("src.rules.sync.httpx.AsyncClient", return_value=client):
        await sync_rules_from_remote(
            "https://example.test/rules?token=synthetic",
            rules,
            users,
            expected_account_id=42,
        )

    users.replace_rules_and_settings.assert_not_awaited()
    assert len(requests) == 1
    users.save_user.assert_not_awaited()
    rules.delete_all.assert_not_awaited()
    rules.add.assert_not_awaited()
