"""Which text of a turn streams live: text blocks and `send_message`'s text only."""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterable
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from harmony.llm.outgoing import OutgoingText, partial_string_field


def _start(index: int, block: dict[str, Any]) -> dict[str, Any]:
    return {"type": "content_block_start", "index": index, "content_block": block}


def _delta(index: int, delta: dict[str, Any]) -> dict[str, Any]:
    return {"type": "content_block_delta", "index": index, "delta": delta}


def _tool_events(
    index: int, name: str, payload: dict[str, Any], chunk: int
) -> list[dict[str, Any]]:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    events = [_start(index, {"type": "tool_use", "id": "toolu_1", "name": name, "input": {}})]
    events += [
        _delta(index, {"type": "input_json_delta", "partial_json": raw[i : i + chunk]})
        for i in range(0, len(raw), chunk)
    ]
    return events


def _feed(events: Iterable[dict[str, Any]]) -> list[str]:
    outgoing = OutgoingText()
    return [piece for event in events if (piece := outgoing.feed(event))]


def test_text_blocks_stream_and_thinking_does_not() -> None:
    events = [
        _start(0, {"type": "thinking", "thinking": "", "signature": ""}),
        _delta(0, {"type": "thinking_delta", "thinking": "private reasoning"}),
        _start(1, {"type": "text", "text": ""}),
        _delta(1, {"type": "text_delta", "text": "Hello, "}),
        _delta(1, {"type": "text_delta", "text": "Henderson."}),
        {"type": "message_stop"},
    ]
    assert _feed(events) == ["Hello, ", "Henderson."]


@pytest.mark.parametrize("chunk", [1, 3, 7, 1000])
def test_send_message_text_streams_as_its_input_arrives(chunk: int) -> None:
    body = 'Quote: "$1,180" \\ net 30\nThanks — Northlake 🔬'
    events = _tool_events(0, "send_message", {"text": body, "cc": "ignored"}, chunk)
    pieces = _feed(events)
    assert "".join(pieces) == body
    assert len(pieces) > 1 or chunk == 1000


def test_other_tools_and_fields_never_stream() -> None:
    events = _tool_events(0, "consult", {"question": "Can we do Nov 6?"}, 4)
    events += _tool_events(1, "send_message", {"to": "x", "subject": "y"}, 4)
    assert _feed(events) == []


@given(st.text(), st.integers(min_value=1, max_value=12))
def test_any_body_round_trips_in_any_chunking(body: str, chunk: int) -> None:
    raw = json.dumps({"body": body}, separators=(",", ":"))  # ASCII escapes everywhere
    pieces = [partial_string_field(raw[:end], "body") or "" for end in range(len(raw) + 1)]
    assert pieces[-1] == body
    # The decoded prefix only ever grows: nothing shown is later taken back.
    for before, after in itertools.pairwise(pieces):
        assert after.startswith(before)
    events = _tool_events(0, "send_message", {"text": body}, chunk)
    assert "".join(_feed(events)) == body


def test_an_unfinished_escape_waits_for_the_rest() -> None:
    assert partial_string_field('{"body":"a\\', "body") == "a"
    assert partial_string_field('{"body":"a\\u00', "body") == "a"
    assert partial_string_field('{"body":"a\\ud83d', "body") == "a"
    assert partial_string_field('{"body":"a\\ud83d\\udd2c', "body") == "a🔬"
    assert partial_string_field('{"bo', "body") is None
