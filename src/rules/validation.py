from src.requests import AutoreactConfigRequest, BadRequest
from src.rules.models import DEFAULT_AUTOREACT_EMOJI, Rule


def stored_autoreact_options(rule: Rule) -> tuple[str, list[int]]:
    """Validate saved options while preserving legacy omitted defaults."""
    if not isinstance(rule.config, dict):
        raise BadRequest("Invalid autoreact configuration")
    emoji = rule.config.get("emoji", DEFAULT_AUTOREACT_EMOJI)
    targets = rule.config.get("target_users")
    if targets is None:
        targets = []
    request = AutoreactConfigRequest.from_json(
        {
            "chat_id": rule.chat_id,
            "topic_id": rule.topic_id,
            "enabled": True,
            "config": {"emoji": emoji, "target_users": targets},
        }
    )
    return request.config["emoji"], request.config["target_users"]
