from dataclasses import dataclass
import re
from typing import Any, Optional

import regex


class BadRequest(ValueError):
    pass


def require_body(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise BadRequest("JSON object required")
    return data


def reject_unknown(data: dict[str, Any], allowed: set[str]) -> None:
    if not data.keys() <= allowed:
        raise BadRequest("Unknown request field")


def require_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if value is None or value == "":
        raise BadRequest(f"{key} required")
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,19}", value, re.ASCII):
        result = int(value)
    else:
        raise BadRequest(f"{key} must be an integer")
    if not -(2**63) <= result < 2**63 or result == 0:
        raise BadRequest(f"{key} is out of range")
    if key in {"topic_id", "msg_id"} and not 0 < result < 2**31:
        raise BadRequest(f"{key} must be a positive Telegram message ID")
    return result


def optional_int(data: dict[str, Any], key: str) -> Optional[int]:
    value = data.get(key)
    if value is None:
        return None
    return require_int(data, key)


def bool_value(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if type(value) is not bool:
        raise BadRequest(f"{key} must be a boolean")
    return value


@dataclass(frozen=True)
class SettingsPatch:
    values: dict[str, Any]

    @classmethod
    def from_json(cls, data: Any) -> "SettingsPatch":
        values = require_body(data)
        bool_fields = {
            "autoread_service_messages",
            "autoread_polls",
            "autoread_self",
            "debug_mode",
        }
        text_limits = {
            "autoread_bots": 4096,
            "autoread_regex": 1024,
            "ai_provider": 32,
            "ai_model": 256,
            "ai_api_key": 4096,
            "ai_prompt": 16384,
        }
        if not values or not values.keys() <= bool_fields | text_limits.keys():
            raise BadRequest("Unknown or empty settings")
        for key, value in values.items():
            if key in bool_fields:
                bool_value(values, key)
            elif value is None and key.startswith("ai_"):
                continue
            elif not isinstance(value, str) or len(value) > text_limits[key]:
                raise BadRequest(f"Invalid {key}")
        if values.get("ai_provider") not in (None, "", "gemini"):
            raise BadRequest("Only Gemini is supported")
        if values.get("autoread_regex"):
            try:
                regex.compile(values["autoread_regex"], regex.VERSION0)
            except regex.error as exc:
                raise BadRequest("Invalid regular expression") from exc
        return cls(dict(values))

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
        reject_unknown(body, {"chat_id", "topic_id", "enabled"})
        return cls(
            chat_id=require_int(body, "chat_id"),
            topic_id=optional_int(body, "topic_id"),
            enabled=bool_value(body, "enabled"),
        )


@dataclass(frozen=True)
class ApplyAllTopicsRequest:
    forum_id: int
    enabled: bool

    @classmethod
    def from_json(cls, data: Any) -> "ApplyAllTopicsRequest":
        body = require_body(data)
        reject_unknown(body, {"forum_id", "enabled"})
        return cls(
            forum_id=require_int(body, "forum_id"),
            enabled=bool_value(body, "enabled"),
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
        reject_unknown(body, {"chat_id", "topic_id", "enabled", "config"})
        enabled = bool_value(body, "enabled")
        config = body.get("config", {})
        if not isinstance(config, dict):
            raise BadRequest("config must be an object")
        reject_unknown(config, {"emoji", "target_users"})
        if enabled and config.keys() != {"emoji", "target_users"}:
            raise BadRequest("Enabled autoreact requires emoji and target_users")
        if "emoji" in config:
            validate_reaction(config["emoji"])
        if "target_users" in config:
            target_users = config["target_users"]
            if (
                not isinstance(target_users, list)
                or len(target_users) > 1000
                or not all(
                    type(user_id) is int
                    and -(2**63) <= user_id < 2**63
                    and user_id != 0
                    for user_id in target_users
                )
            ):
                raise BadRequest(
                    "target_users must be a list of integers within Telegram's ID range"
                )
        return cls(
            chat_id=require_int(body, "chat_id"),
            topic_id=optional_int(body, "topic_id"),
            enabled=enabled,
            config=config,
        )


@dataclass(frozen=True)
class DebugProcessRequest:
    chat_id: int
    msg_id: int
    topic_id: Optional[int]
    evaluate_ai: bool

    @classmethod
    def from_json(cls, data: Any) -> "DebugProcessRequest":
        body = require_body(data)
        reject_unknown(body, {"chat_id", "msg_id", "topic_id", "evaluate_ai"})
        return cls(
            chat_id=require_int(body, "chat_id"),
            msg_id=require_int(body, "msg_id"),
            topic_id=optional_int(body, "topic_id"),
            evaluate_ai=bool_value(body, "evaluate_ai")
            if "evaluate_ai" in body
            else False,
        )


@dataclass(frozen=True)
class MarkReadRequest:
    topic_id: Optional[int]

    @classmethod
    def from_json(cls, data: Any) -> "MarkReadRequest":
        body = require_body(data)
        reject_unknown(body, {"topic_id"})
        return cls(topic_id=optional_int(body, "topic_id"))


def validate_reaction(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 32:
        raise BadRequest("Invalid reaction")
    if re.fullmatch(r"[0-9]+", value, re.ASCII):
        if len(value) > 19 or not 0 < int(value) < 2**63:
            raise BadRequest("Invalid custom reaction ID")
        return value
    if (
        regex.fullmatch(r"\X", value) is None
        or regex.search(r"\p{Cc}|\p{Cs}", value)
        or not regex.search(
            r"\p{Extended_Pictographic}|\p{Regional_Indicator}|\u20e3", value
        )
    ):
        raise BadRequest("Reaction must be an emoji or a custom reaction ID")
    return value


@dataclass(frozen=True)
class ReactionRequest:
    reaction: str

    @classmethod
    def from_json(cls, data: Any) -> "ReactionRequest":
        body = require_body(data)
        reject_unknown(body, {"reaction"})
        return cls(reaction=validate_reaction(body.get("reaction")))


@dataclass(frozen=True)
class PasswordRequest:
    password: str

    @classmethod
    def from_json(cls, data: Any) -> "PasswordRequest":
        body = require_body(data)
        reject_unknown(body, {"password", "attempt_id"})
        password = body.get("password")
        if not isinstance(password, str) or not password or len(password) > 1024:
            raise BadRequest("password required")
        return cls(password=password)
