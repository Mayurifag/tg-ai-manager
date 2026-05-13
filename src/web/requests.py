from dataclasses import dataclass
from typing import Any, Optional


class BadRequest(ValueError):
    pass


def require_body(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise BadRequest("JSON object required")
    return data


def require_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if value is None or value == "":
        raise BadRequest(f"{key} required")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BadRequest(f"{key} must be an integer") from exc


def optional_int(data: dict[str, Any], key: str) -> Optional[int]:
    value = data.get(key)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BadRequest(f"{key} must be an integer") from exc


def bool_value(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


@dataclass(frozen=True)
class SettingsPatch:
    values: dict[str, Any]

    @classmethod
    def from_json(cls, data: Any) -> "SettingsPatch":
        return cls(require_body(data))

    def get(self, key: str, default: Any) -> Any:
        return self.values[key] if key in self.values else default


@dataclass(frozen=True)
class ToggleRuleRequest:
    chat_id: int
    topic_id: Optional[int]
    enabled: bool

    @classmethod
    def from_json(cls, data: Any) -> "ToggleRuleRequest":
        body = require_body(data)
        return cls(
            chat_id=require_int(body, "chat_id"),
            topic_id=optional_int(body, "topic_id"),
            enabled=bool_value(body, "enabled", True),
        )


@dataclass(frozen=True)
class ApplyAllTopicsRequest:
    forum_id: int
    enabled: bool

    @classmethod
    def from_json(cls, data: Any) -> "ApplyAllTopicsRequest":
        body = require_body(data)
        return cls(
            forum_id=require_int(body, "forum_id"),
            enabled=bool_value(body, "enabled", True),
        )


@dataclass(frozen=True)
class AutoreactConfigRequest:
    chat_id: int
    topic_id: Optional[int]
    enabled: bool
    config: dict[str, Any]

    @classmethod
    def from_json(cls, data: Any) -> "AutoreactConfigRequest":
        body = require_body(data)
        config = body.get("config", {})
        if not isinstance(config, dict):
            raise BadRequest("config must be an object")
        target_users = config.get("target_users")
        if target_users is not None:
            if not isinstance(target_users, list) or not all(
                type(user_id) is int for user_id in target_users
            ):
                raise BadRequest("target_users must be a list of integers")
        return cls(
            chat_id=require_int(body, "chat_id"),
            topic_id=optional_int(body, "topic_id"),
            enabled=bool_value(body, "enabled", False),
            config=config,
        )


@dataclass(frozen=True)
class DebugProcessRequest:
    chat_id: int
    msg_id: int

    @classmethod
    def from_json(cls, data: Any) -> "DebugProcessRequest":
        body = require_body(data)
        return cls(
            chat_id=require_int(body, "chat_id"), msg_id=require_int(body, "msg_id")
        )


@dataclass(frozen=True)
class MarkReadRequest:
    topic_id: Optional[int]

    @classmethod
    def from_json(cls, data: Any) -> "MarkReadRequest":
        body = data if isinstance(data, dict) else {}
        return cls(topic_id=optional_int(body, "topic_id"))


@dataclass(frozen=True)
class ReactionRequest:
    reaction: str

    @classmethod
    def from_json(cls, data: Any) -> "ReactionRequest":
        body = require_body(data)
        reaction = body.get("reaction")
        if not reaction:
            raise BadRequest("reaction required")
        return cls(reaction=str(reaction))


@dataclass(frozen=True)
class PasswordRequest:
    password: str

    @classmethod
    def from_json(cls, data: Any) -> "PasswordRequest":
        body = require_body(data)
        password = body.get("password")
        if not password:
            raise BadRequest("password required")
        return cls(password=str(password))
