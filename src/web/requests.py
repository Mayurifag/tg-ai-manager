"""Web-facing imports of the shared request validators."""

from typing import Any

from quart import Request, request
from quart.json.provider import DefaultJSONProvider

from src.infrastructure.json_data import load_json
from src.requests import (
    ApplyAllTopicsRequest as ApplyAllTopicsRequest,
    AutoreactConfigRequest as AutoreactConfigRequest,
    BadRequest as BadRequest,
    DebugProcessRequest as DebugProcessRequest,
    MarkReadRequest as MarkReadRequest,
    PasswordRequest as PasswordRequest,
    ReactionRequest as ReactionRequest,
    SettingsPatch as SettingsPatch,
    ToggleRuleRequest as ToggleRuleRequest,
)


class ValidatedRequest(Request):
    async def get_json(
        self, force: bool = False, silent: bool = False, cache: bool = True
    ) -> Any:
        try:
            return await super().get_json(force=force, silent=silent, cache=cache)
        except UnicodeDecodeError as error:
            # Quart 0.23 decodes before its JSON error handler. Invalid UTF-8
            # is a bad request, not a server or credential-storage failure.
            if silent:
                return None
            return self.on_json_loading_failed(error)


def _reject_request_constant(_value: str) -> None:
    # Python's JSON decoder otherwise accepts these non-JSON numeric literals.
    raise ValueError("Invalid JSON numeric constant")


class ValidatedJSONProvider(DefaultJSONProvider):
    def loads(self, value, **kwargs):
        kwargs.setdefault("parse_constant", _reject_request_constant)
        return load_json(value, **kwargs)


async def optional_empty_json_body() -> None:
    """Allow an absent body or an empty JSON object on bodyless mutations."""
    if not await request.get_data(cache=True):
        return
    if not request.is_json or await request.get_json() != {}:
        raise BadRequest("Expected an empty JSON object")


def query_values(allowed: set[str]) -> dict[str, str]:
    """Reject unknown or repeated parameters before selecting their values."""
    if not set(request.args).issubset(allowed) or any(
        len(request.args.getlist(key)) != 1 for key in request.args
    ):
        raise BadRequest("Invalid query parameters")
    return request.args.to_dict()
