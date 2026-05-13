import pytest

from src.web.requests import AutoreactConfigRequest, BadRequest


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
