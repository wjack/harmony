"""Wire-format helpers shared by the builders, clients and checkers.

`canonical_json` is the one serialization of a request body: compact, key order as
built (never sorted, so echoed model output keeps the order the API returned), and
UTF-8 rather than `\\u` escapes. Consecutive requests for one (thread, side) must be
byte-prefix extensions of each other in this form (`llm/contract.py`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from pydantic import JsonValue

type JsonObject = dict[str, JsonValue]


def canonical_json(value: JsonValue | Mapping[str, JsonValue]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def as_object(value: JsonValue | None) -> JsonObject | None:
    return value if isinstance(value, dict) else None


def as_list(value: JsonValue | None) -> list[JsonValue] | None:
    return value if isinstance(value, list) else None


def as_str(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) else None


def as_int(value: JsonValue | None) -> int | None:
    # bool is an int subclass; a token count is never a bool.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def objects(value: JsonValue | None) -> list[JsonObject]:
    """The dict items of a JSON list (non-dicts skipped); [] for anything else."""
    items = as_list(value) or []
    return [item for item in items if isinstance(item, dict)]
