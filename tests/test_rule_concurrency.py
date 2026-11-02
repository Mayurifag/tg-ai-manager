import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.rules.models import Rule, RuleType
from src.rules.ports import InheritedRuleError
from src.rules.service import RuleService
from src.users.models import User
from src.web.routes import register_routes
from tests.test_sync_atomic import repositories as repositories


@pytest.fixture
async def service(repositories):
    rules, users = repositories
    await users.save_user(User(session_string="synthetic-session"))
    return RuleService(rules, AsyncMock(), MagicMock(), users)


async def test_concurrent_enable_is_atomic_and_idempotent(service):
    results = await asyncio.gather(
        *(service.toggle_autoread(100, 7, True) for _ in range(20))
    )
    assert len({result.id for result in results}) == 1
    assert len(await service.rule_repo.get_all()) == 1


async def test_disabling_inherited_topic_is_rejected_without_changing_rules(service):
    await service.toggle_autoread(100, None, True)
    await service.toggle_autoread(100, 7, True)
    with pytest.raises(InheritedRuleError):
        await service.toggle_autoread(100, 7, False)
    assert len(await service.rule_repo.get_all()) == 2
    assert await service.is_autoread_enabled(100, 7)


async def test_bulk_rules_do_not_duplicate_topics_and_disable_every_scope(service):
    await service.apply_autoread_to_all_topics(100, True)
    assert [
        (rule.chat_id, rule.topic_id) for rule in await service.rule_repo.get_all()
    ] == [(100, None)]
    for topic_id in range(1, 105):
        await service.toggle_autoread(100, topic_id, True)
    await service.rule_repo.add(
        Rule(chat_id=100, rule_type=RuleType.AI_AUTOREAD, topic_id=4)
    )
    await service.apply_autoread_to_all_topics(100, False)
    remaining = await service.rule_repo.get_all()
    assert [(rule.rule_type, rule.topic_id) for rule in remaining] == [
        (RuleType.AI_AUTOREAD, 4)
    ]


async def test_api_reports_inherited_disable_without_claiming_success(service):
    await service.toggle_autoread(100, None, True)
    app = Quart(__name__)
    app.login_lock = asyncio.Lock()
    app.rule_service = service
    app.tg_adapter = MagicMock()
    app.tg_adapter.is_connected.return_value = True
    register_routes(app)
    response = await app.test_client().post(
        "/api/rules/autoread/toggle",
        json={"chat_id": 100, "topic_id": 7, "enabled": False},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert response.status_code == 409
    assert "inherits" in (await response.get_json())["error"]
    assert await service.is_autoread_enabled(100, 7)
