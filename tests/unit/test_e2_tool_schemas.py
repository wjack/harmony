"""Tool definitions agree with their input models, and the answerer's, sharing
check's and screener's output schemas stay within what structured outputs support
(ARCHITECTURE.md §5)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import UnionType
from typing import Any, Final, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from harmony.agents.consult import (
    CONSULT_OUTPUT_SCHEMA,
    SHARING_CHECK_OUTPUT_SCHEMA,
    ConsultDraft,
    SharingCheck,
)
from harmony.agents.discovery import SCREENER_OUTPUT_SCHEMA, Verdict
from harmony.agents.runtime import PerformativeName
from harmony.agents.tools.internal import ANSWERER_TOOLS, INTERNAL_INPUTS, INTERNAL_TOOLS
from harmony.agents.tools.rep import REP_INPUTS, REP_TOOLS

TOOLS: Final = [(tool, REP_INPUTS) for tool in REP_TOOLS] + [
    (tool, INTERNAL_INPUTS) for tool in INTERNAL_TOOLS
]
# JSON schema keywords structured outputs reject (claude-api skill, Structured Outputs).
UNSUPPORTED: Final = frozenset(
    {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
    | {"minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems"}
)
OUTPUT_SCHEMAS: Final = [
    (CONSULT_OUTPUT_SCHEMA, ConsultDraft),
    (SHARING_CHECK_OUTPUT_SCHEMA, SharingCheck),
    (SCREENER_OUTPUT_SCHEMA, Verdict),
]


def _name(tool: Mapping[str, Any]) -> str:
    return str(tool["name"])


def _types(annotation: Any) -> set[str]:
    """The JSON types a field annotation accepts (enough to compare with a schema)."""
    names = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }
    found: set[str] = set()
    union = get_origin(annotation) in (Union, UnionType)
    for arg in get_args(annotation) if union else (annotation,):
        origin = get_origin(arg) or arg
        if arg is type(None):
            continue
        if isinstance(origin, type) and issubclass(origin, BaseModel):
            found.add("object")
        elif origin in names:
            found.add(names[origin])
        else:
            found.add("string")  # dates, literals, UUID-shaped references
    return found


@pytest.mark.parametrize(("tool", "inputs"), TOOLS, ids=lambda value: str(value)[:40])
def test_each_tool_matches_its_input_model(
    tool: Mapping[str, Any], inputs: Mapping[str, type[BaseModel]]
) -> None:
    model = inputs[_name(tool)]
    schema = tool["input_schema"]
    assert schema["additionalProperties"] is False
    properties: dict[str, Any] = schema["properties"]
    assert set(properties) == set(model.model_fields)
    required = {name for name, field in model.model_fields.items() if field.is_required()}
    assert set(schema["required"]) == required
    for name, field in model.model_fields.items():
        declared = properties[name].get("type")
        assert declared in _types(field.annotation), (name, declared)


def test_tool_lists_and_input_maps_name_the_same_tools() -> None:
    assert [_name(tool) for tool in REP_TOOLS] == list(REP_INPUTS)
    assert set(REP_INPUTS) == set(get_args(PerformativeName))
    assert [_name(tool) for tool in INTERNAL_TOOLS] == list(INTERNAL_INPUTS)
    # The answerer reads the org's records and nothing else (R§4.1).
    assert [_name(tool) for tool in ANSWERER_TOOLS] == ["search_org_knowledge", "read_document"]


def test_every_representative_gets_the_same_three_tools() -> None:
    assert [_name(tool) for tool in REP_TOOLS] == ["send_message", "consult", "close"]
    assert {name: list(model.model_fields) for name, model in REP_INPUTS.items()} == {
        "send_message": ["text"],
        "consult": ["question"],
        "close": ["summary"],
    }


def _objects(schema: Any) -> Iterator[dict[str, Any]]:
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for value in schema.values():
            yield from _objects(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _objects(value)


def _keys(schema: Any) -> Iterator[str]:
    if isinstance(schema, dict):
        for key, value in schema.items():
            yield key
            if key != "properties":
                yield from _keys(value)
            else:
                for nested in value.values():
                    yield from _keys(nested)
    elif isinstance(schema, list):
        for value in schema:
            yield from _keys(value)


@pytest.mark.parametrize(
    ("schema", "model"), OUTPUT_SCHEMAS, ids=lambda v: getattr(v, "__name__", "")
)
def test_output_schemas_use_only_supported_keywords(schema: Any, model: type[BaseModel]) -> None:
    for obj in _objects(schema):
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    assert not UNSUPPORTED & set(_keys(schema))
    # Nullable fields are anyOf, never a list of types.
    assert all(not isinstance(obj.get("type"), list) for obj in _objects(schema))
    assert set(schema["properties"]) == set(model.model_fields)


def test_the_draft_has_only_the_fields_the_spec_names() -> None:
    assert list(ConsultDraft.model_fields) == [
        "answer",
        "needs_human",
        "needs_human_reason",
        "route",
        "rationale",
    ]
