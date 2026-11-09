import json
from typing import Any


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object field")
        result[key] = value
    return result


def load_json(value: str | bytes | bytearray, **kwargs: Any) -> Any:
    """Reject ambiguous object fields and excessively nested JSON."""
    kwargs.setdefault("object_pairs_hook", _unique_object)
    try:
        return json.loads(value, **kwargs)
    except RecursionError:
        raise ValueError("JSON nesting is too deep") from None
