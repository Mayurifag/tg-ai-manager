import pytest

from src.web.requests import (
    AutoreactConfigRequest,
    BadRequest,
    MarkReadRequest,
    ReactionRequest,
    SettingsPatch,
    ToggleRuleRequest,
)


def test_autoreact_config_accepts_integer_targets():
    request = AutoreactConfigRequest.from_json(
        {
            "chat_id": 100,
            "enabled": True,
            "config": {"emoji": "👍", "target_users": [1, 2]},
        }
    )

    assert request.config["target_users"] == [1, 2]


@pytest.mark.parametrize("target_users", [["1"], [True], "1"])
def test_autoreact_config_rejects_non_integer_targets(target_users):
    with pytest.raises(BadRequest, match="target_users must be a list of integers"):
        AutoreactConfigRequest.from_json(
            {
                "chat_id": 100,
                "enabled": True,
                "config": {"emoji": "👍", "target_users": target_users},
            }
        )


@pytest.mark.parametrize("value", [True, 1.9, "1.9", "abc", 0, 2**63])
def test_rule_requests_reject_invalid_chat_ids(value):
    with pytest.raises(BadRequest):
        ToggleRuleRequest.from_json({"chat_id": value, "enabled": True})


@pytest.mark.parametrize("enabled", [None, "false", 0, 1])
def test_rule_requests_require_explicit_boolean_enabled(enabled):
    with pytest.raises(BadRequest):
        ToggleRuleRequest.from_json({"chat_id": -100, "enabled": enabled})
    with pytest.raises(BadRequest):
        ToggleRuleRequest.from_json({"chat_id": -100})


@pytest.mark.parametrize(
    "body",
    [
        [],
        "bad",
        None,
        {"topic_id": -1},
        {"topic_id": True},
        {"topic": 123},
        {"topic_id": 2**31},
    ],
)
def test_read_request_does_not_widen_malformed_scope(body):
    with pytest.raises(BadRequest):
        MarkReadRequest.from_json(body)


@pytest.mark.parametrize(
    "body",
    [
        {"debug_mode": "false"},
        {"autoread_polls": 1},
        {"ai_provider": "openai"},
        {"autoread_bots": None},
        {"unknown_setting": True},
        {"autoread_regex": "("},
    ],
)
def test_settings_reject_invalid_values(body):
    with pytest.raises(BadRequest):
        SettingsPatch.from_json(body)


@pytest.mark.parametrize(
    "reaction", [True, 0, None, "", "a", "👍❤️", "0", str(2**63), "👍\n"]
)
def test_reaction_rejects_malformed_values(reaction):
    with pytest.raises(BadRequest):
        ReactionRequest.from_json({"reaction": reaction})


@pytest.mark.parametrize(
    "reaction", ["👍", "❤️", "👩🏽‍💻", "🇺🇦", "1️⃣", "9223372036854775807"]
)
def test_reaction_accepts_emoji_sequences_and_bounded_custom_ids(reaction):
    assert ReactionRequest.from_json({"reaction": reaction}).reaction == reaction


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"emoji": "👍"},
        {"target_users": []},
        {"emoji": "bad", "target_users": []},
        {"emoji": "👍", "target_users": [0]},
        {"emoji": "👍", "target_users": [], "scope": "all"},
    ],
)
def test_enabling_autoreact_requires_explicit_valid_configuration(config):
    with pytest.raises(BadRequest):
        AutoreactConfigRequest.from_json(
            {"chat_id": 100, "enabled": True, "config": config}
        )
