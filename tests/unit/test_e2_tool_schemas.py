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
from harmony.agents.tools.internal import (
    ANSWERER_TOOLS,
    AUTO_SEND_MAX_RECIPIENTS,
    GET_REQUEST_STATUS_TOOL,
    INTERNAL_INPUTS,
    INTERNAL_TOOLS,
    SEND_NETWORK_REQUEST_TOOL,
    TITLE_MAX_CHARS,
    RecipientInput,
    SendNetworkRequestInput,
)
from harmony.agents.tools.rep import (
    CLOSE_TOOL,
    CONSULT_AFTER_SEND,
    CONSULT_TOOL,
    NOT_REPLIED,
    REP_INPUTS,
    REP_TOOLS,
    SEND_MESSAGE_TOOL,
)
from harmony.temporal.workflows.payloads import NetworkRequestInput

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


def test_a_request_names_itself_and_asks_each_recipient_its_own_part() -> None:
    """3.13 E2, E4, E7, E13: required in the schema and enforced by the input models,
    since the tools aren't strict."""
    schema: Any = SEND_NETWORK_REQUEST_TOOL["input_schema"]
    items = schema["properties"]["recipients"]["items"]
    assert set(items["required"]) == {"org", "reason", "title", "ask", "suggested"}
    assert set(schema["required"]) == {"title", "brief", "recipients"}
    for title in (schema["properties"]["title"], items["properties"]["title"]):
        assert title["maxLength"] == TITLE_MAX_CHARS == 60
    assert "never a budget" in items["properties"]["title"]["description"]
    assert "nothing about the other organizations" in items["properties"]["ask"]["description"]
    assert "starts it unticked" in items["properties"]["suggested"]["description"]
    assert "Put each organization's part" in schema["properties"]["brief"]["description"]
    for model in (RecipientInput, SendNetworkRequestInput):
        assert all(field.is_required() for field in model.model_fields.values())


def test_the_request_tool_is_a_harmony_network_request_in_either_send_mode() -> None:
    description = str(SEND_NETWORK_REQUEST_TOOL["description"])
    assert description.startswith("Propose a Harmony Network Request")
    # Mode-neutral (3.10): the tool bytes never depend on the chat's setting.
    assert "in a chat set to send without review it goes out at once" in description
    assert "The result says which" in description
    assert "One need is one request" in description


def test_the_status_tool_has_nothing_to_check_right_after_a_send() -> None:
    """Decision 7. Not "only when asked": the duplicate guard points Claude at it."""
    description = str(GET_REQUEST_STATUS_TOOL["description"])
    assert (
        "Right after a request goes out there's nothing to check, and no need to promise "
        "to check back: you're told in the chat when something needs the member and when "
        "every conversation has ended." in description
    )
    assert "only when" not in description


def test_a_request_sent_without_review_opens_every_conversation_at_once() -> None:
    default = NetworkRequestInput.model_fields["max_active_threads"].default
    assert default == AUTO_SEND_MAX_RECIPIENTS


def test_the_representative_is_told_how_to_wait_and_what_a_message_is() -> None:
    """3.13 E1, E3."""
    close = str(CLOSE_TOOL["description"])
    assert close.startswith("End the conversation for both sides")
    assert "Not for waiting: to wait for their reply, stop calling tools." in close
    # A member can reopen a closed conversation (decision 15).
    assert "for good" not in close
    assert "for good" not in NOT_REPLIED
    send = str(SEND_MESSAGE_TOOL["description"])
    assert "They read the text exactly as written." in send
    assert "Never send notes about the conversation" in send


def test_the_representative_consults_before_it_replies() -> None:
    """Decision 1: a consult after a send is refused, and the tool says when to ask."""
    consult = str(CONSULT_TOOL["description"])
    assert (
        "Consult before you reply: once you've sent a message this turn, it's the "
        "counterparty's move, so ask at the start of your next turn, after theirs." in consult
    )
    refusal = CONSULT_AFTER_SEND.format(counterparty="Northlake")
    assert refusal.startswith("Not asked: your message already went out, so it's Northlake's")
    assert "Don't close. End your turn now: stop calling tools." in refusal


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
        "short_question",
    ]
