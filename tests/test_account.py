import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.application.interactors import ChatInteractor
from src.users.models import User
from src.web import create_app


class AccountScope(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = create_app()
        self.app.before_serving_funcs.clear()
        self.app.after_serving_funcs.clear()
        self.adapter = SimpleNamespace(
            _retired=False,
            account_token="current",
            is_connected=lambda: True,
            mark_as_read=AsyncMock(),
            display_name="Example Person",
            profile_avatar_id=42,
            get_chat_avatar=AsyncMock(return_value=b"avatar"),
        )
        self.app.tg_adapter = self.adapter
        self.app.chat_interactor = ChatInteractor(self.adapter, None, None)
        self.user = User(username="@example", session_string="fixture", telegram_id=42)
        self.app.user_repo = SimpleNamespace(get_user=AsyncMock(return_value=self.user))
        self.client = self.app.test_client()

    @staticmethod
    def headers(token):
        return {"X-Requested-With": "XMLHttpRequest", "X-Account-Token": token}

    async def test_stale_page_cannot_read_or_reset_replacement_account(self):
        for path in ("/api/chat/42/read", "/api/settings/reset"):
            result = await self.client.post(path, headers=self.headers("old"), json={})
            self.assertEqual(result.status_code, 409)
        self.adapter.mark_as_read.assert_not_called()
        result = await self.client.post(
            "/api/chat/42/read", headers=self.headers("current"), json={}
        )
        self.assertEqual(result.status_code, 202)
        self.adapter.mark_as_read.assert_awaited_once()

    async def test_stale_page_cannot_subscribe_to_replacement_account(self):
        result = await self.client.get(
            "/api/events/stream?account=old", headers={"Accept": "text/event-stream"}
        )
        self.assertIn(b"account_changed", await result.get_data())
        self.assertEqual(self.app.events.client_count, 0)

    async def test_saved_identity_allows_qr_start_without_old_connection(self):
        self.adapter.is_connected = lambda: False
        new_adapter = SimpleNamespace(
            _retired=False,
            account_token="new-attempt",
            start_qr_login=AsyncMock(return_value="fixture-qr"),
        )

        async def replace(**_):
            self.app.tg_adapter = new_adapter

        with patch("src.web.routes.auth.reload_tg_adapter", replace):
            result = await self.client.post(
                "/api/auth/qr/start", headers=self.headers("current"), json={}
            )
        self.assertEqual(result.status_code, 200, await result.get_data())
        self.assertEqual(new_adapter._expected_login_account_id, 42)
        self.assertEqual((await result.get_json())["account_token"], "new-attempt")

    async def test_sidebar_profile(self):
        self.adapter.stickers = SimpleNamespace(
            check=lambda: None, prefetch=lambda: None
        )
        page = await self.client.get("/stickers")
        html = await page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        for content in ("Example Person", "@example", "/media/avatar/42"):
            self.assertIn(content, html)
        image = await self.client.get("/media/avatar/42")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(await image.get_data(), b"avatar")

    async def test_asset_version_updates_without_recompiling_template(self):
        with patch("src.jinja_filters.os.path.getmtime", return_value=100) as mtime:
            template = self.app.jinja_env.from_string(
                '{{ "static/js/stickers.js" | file_mtime }}'
            )
            self.assertEqual(await template.render_async(), "100")
            mtime.return_value = 200
            self.assertEqual(await template.render_async(), "200")

    async def test_rule_import_requires_explicit_confirmation(self):
        with patch("src.web.routes.settings.import_rules_from_remote") as importer:
            result = await self.client.post(
                "/api/rules/import",
                headers=self.headers("current"),
                json={"replace": 1},
            )
        self.assertEqual(result.status_code, 400)
        importer.assert_not_called()
