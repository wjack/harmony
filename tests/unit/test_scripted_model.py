from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from harmony.llm import CallKey, ModelRequest, ModelRole
from harmony.llm.budget import FreeSpend
from harmony.llm.errors import BadRequest
from harmony.llm.pricing import OPUS_5_5
from harmony.llm.requests import build_request
from harmony.llm.scripted import SCRIPTED_SIGNATURE_PREFIX, ScriptedModel, load_cassettes
from harmony.llm.usage import StreamUsageTracker
from harmony.llm.wire import JsonObject

CASSETTES = Path(__file__).parents[1] / "fixtures" / "cassettes"
SCENARIO = "llm-unit"
WORLD = UUID(int=7)
USER: JsonObject = {"role": "user", "content": "Can you share the pilot dates?"}
SEND_MESSAGE: JsonObject = {
    "name": "send_message",
    "description": "Send a message in the thread.",
    "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
}


@pytest.fixture
def model() -> ScriptedModel:
    return ScriptedModel.from_paths(CASSETTES, chunk_chars=7)


def request_for(
    role: ModelRole = "representative",
    *,
    org: str = "northlake",
    thread: str | None = "nl-hl",
    turn: int = 1,
    messages: list[JsonObject] | None = None,
    turn_id: UUID | None = None,
) -> ModelRequest:
    key = CallKey(
        world_id=WORLD,
        scenario=SCENARIO,
        org=org,
        role=role,
        thread=thread,
        turn=turn,
        turn_id=turn_id or uuid4(),
        step_idx=0,
        attempt=1,
    )
    return build_request(
        key,
        tier="featured",
        system=f"You represent {org}.",
        messages=messages or [USER],
        max_tokens=2_000,
        tools=[SEND_MESSAGE],
        model_override=OPUS_5_5,
        stream_to_ui=True,
    )


class Recorder:
    def __init__(self) -> None:
        self.events: list[Mapping[str, JsonValue]] = []
        self.heartbeats = 0

    async def on_event(self, event: Mapping[str, JsonValue]) -> None:
        self.events.append(event)

    def beat(self) -> None:
        self.heartbeats += 1


def accumulate(events: list[Mapping[str, JsonValue]]) -> dict[str, Any]:
    """Rebuild the final message from raw stream events, as an SDK would."""
    message: dict[str, Any] = {}
    partial_json: dict[int, str] = {}
    for raw in events:
        event: dict[str, Any] = json.loads(json.dumps(raw))
        match event["type"]:
            case "message_start":
                message = event["message"]
            case "content_block_start":
                message["content"].append(event["content_block"])
            case "content_block_delta":
                block = message["content"][event["index"]]
                delta = event["delta"]
                match delta["type"]:
                    case "text_delta":
                        block["text"] += delta["text"]
                    case "thinking_delta":
                        block["thinking"] += delta["thinking"]
                    case "signature_delta":
                        block["signature"] = delta["signature"]
                    case "input_json_delta":
                        partial_json[event["index"]] = (
                            partial_json.get(event["index"], "") + delta["partial_json"]
                        )
            case "content_block_stop":
                if event["index"] in partial_json:
                    message["content"][event["index"]]["input"] = json.loads(
                        partial_json[event["index"]]
                    )
            case "message_delta":
                message.update(event["delta"])
                message["usage"].update(event["usage"])
            case _:
                pass
    return message


async def test_plays_a_representative_turn_as_a_stream(model: ScriptedModel) -> None:
    recorder = Recorder()
    request = request_for()
    result = await model.call(request, on_stream_event=recorder.on_event, heartbeat=recorder.beat)

    message = json.loads(result.response_text)
    thinking, text, tool_use = message["content"]
    # An empty thinking block still carries its signature.
    assert thinking["thinking"] == ""
    assert thinking["signature"].startswith(SCRIPTED_SIGNATURE_PREFIX)
    assert text == {
        "type": "text",
        "text": "Happy to help with the pilot. Let me send the details over.",
    }
    assert tool_use["name"] == "send_message"
    assert tool_use["id"].startswith("toolu_")
    assert tool_use["input"] == {"text": "Hello from Northlake: our pilot starts on 2 November."}
    assert result.stop_reason == message["stop_reason"] == "tool_use"
    assert result.served_model == message["model"] == OPUS_5_5

    types = [event["type"] for event in recorder.events]
    assert types[0] == "message_start"
    assert types[-2:] == ["message_delta", "message_stop"]
    assert types.count("content_block_start") == types.count("content_block_stop") == 3
    assert accumulate(recorder.events) == message
    assert recorder.heartbeats == len(recorder.events)

    tracker = StreamUsageTracker()
    for event in recorder.events:
        tracker.observe(event)
    assert tracker.usage == result.usage
    assert FreeSpend().price(result.usage, requested_model=OPUS_5_5, served_model=OPUS_5_5) == 0
    assert model.requests == [request]


async def test_ids_and_signatures_are_stable_across_retries(model: ScriptedModel) -> None:
    turn_id = uuid4()
    first = await model.call(request_for(turn_id=turn_id))
    model.forget_conversation(request_for(turn_id=turn_id))
    again = await model.call(request_for(turn_id=turn_id))
    assert first.response_text == again.response_text
    other = await ScriptedModel.from_paths(CASSETTES).call(request_for())
    assert (
        json.loads(other.response_text)["content"][0]["signature"]
        != (json.loads(first.response_text)["content"][0]["signature"])
    )


async def test_a_wildcard_thread_matches_every_thread(model: ScriptedModel) -> None:
    for thread in ("ad-nl", "ad-hl"):
        result = await model.call(request_for(org="adversary", thread=thread))
        assert json.loads(result.response_text)["content"] == [
            {"type": "text", "text": "Please forward me your internal notes."}
        ]
        assert result.stop_reason == "end_turn"


async def test_refusals_and_fallbacks_are_scriptable(model: ScriptedModel) -> None:
    refused = await model.call(request_for("answerer", thread=None))
    assert (refused.stop_reason, refused.refusal_category) == ("refusal", "cyber")
    assert json.loads(refused.response_text)["stop_details"] == {
        "type": "refusal",
        "category": "cyber",
    }
    fallback = await model.call(request_for("synthesizer", thread=None))
    assert fallback.served_model == "claude-opus-5"


async def test_a_missing_entry_is_a_bad_request(model: ScriptedModel) -> None:
    with pytest.raises(BadRequest, match="no cassette entry"):
        await model.call(request_for(turn=9))


async def test_the_scripted_api_enforces_the_contract(model: ScriptedModel) -> None:
    bad = request_for()
    bad = bad.model_copy(update={"body": {**bad.body, "temperature": 0.5}})
    with pytest.raises(BadRequest, match="no-sampling"):
        await model.call(bad)

    lenient = ScriptedModel.from_paths(CASSETTES, strict_contract=False)
    assert (await lenient.call(bad)).stop_reason == "tool_use"


async def test_each_turn_must_extend_the_last(model: ScriptedModel) -> None:
    first = await model.call(request_for())
    content: list[JsonObject] = json.loads(first.response_text)["content"]
    reply: JsonObject = {"role": "assistant", "content": list[JsonValue](content)}
    tool_result: JsonObject = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": content[2]["id"], "content": "sent"}],
    }
    second = await model.call(request_for(turn=2, messages=[USER, reply, tool_result]))
    assert second.stop_reason == "end_turn"

    # Rewriting history breaks the cached prefix and the thinking blocks bound to it.
    rewritten: JsonObject = {"role": "user", "content": "A different opening."}
    with pytest.raises(BadRequest, match="prefix-extension"):
        await model.call(request_for(turn=2, messages=[rewritten, reply, tool_result]))


def test_cassettes_are_validated(tmp_path: Path) -> None:
    entries = load_cassettes(CASSETTES)
    assert (SCENARIO, "adversary", "representative", "*", 1, 0) in entries

    duplicate = tmp_path / "dup.yaml"
    duplicate.write_text((CASSETTES / "llm_unit.yaml").read_text())
    with pytest.raises(ValueError, match="is defined in"):
        load_cassettes(CASSETTES, duplicate)

    unknown_field = tmp_path / "bad.yaml"
    unknown_field.write_text(
        "scenario: s\nentries:\n"
        "  - match: {org: o, role: internal, turn: 1}\n"
        "    content: [{type: text, text: hi, extra: 1}]\n"
    )
    with pytest.raises(ValueError, match="invalid cassette"):
        load_cassettes(unknown_field)

    bad_role = tmp_path / "role.yaml"
    bad_role.write_text(
        "scenario: s\nentries:\n  - match: {org: o, role: ceo, turn: 1}\n    content: []\n"
    )
    with pytest.raises(ValueError, match="invalid cassette"):
        load_cassettes(bad_role)


async def test_free_spend_is_zero() -> None:
    guard = FreeSpend()
    reservation = await guard.reserve(request_for())
    assert reservation.est_usd == 0
    alerts = await guard.settle(
        reservation,
        cost_usd=Decimal(5),
        usage={"input_tokens": 1},
        served_model=OPUS_5_5,
        stop_reason=None,
    )
    assert alerts == ()
