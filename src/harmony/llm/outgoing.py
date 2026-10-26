"""Which text of a turn is shown live as the model writes it (§5 Streaming, §8).

A representative's turn sends the counterparty either a bare text reply or the `text`
of its `send_message` calls. Clients stream exactly that text: text blocks as their
deltas arrive, and the tool's `text` as its input JSON streams in
(`eager_input_streaming`). Thinking, other tools and other input fields are never
streamed.

`OutgoingText.feed` takes the raw stream events in order and returns the text that
became visible with each one. Partial tool input is decoded only up to the last
complete character, so an escape split across deltas never leaks half-decoded.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from pydantic import JsonValue

from harmony.llm.wire import as_object, as_str

SEND_MESSAGE: Final = "send_message"
TEXT_FIELD: Final = "text"

_ESCAPES: Final = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


@dataclass(slots=True)
class _Block:
    streams_input: bool
    partial_json: str = ""
    shown: int = 0  # characters of the decoded field already returned


@dataclass(slots=True)
class OutgoingText:
    """Follows one model call's stream and returns the outgoing text as it grows."""

    tool: str = SEND_MESSAGE
    field_name: str = TEXT_FIELD
    _blocks: dict[int, _Block] = field(default_factory=dict[int, _Block])

    def feed(self, event: Mapping[str, JsonValue]) -> str:
        """The text that `event` adds to what the counterparty will read ("" if none)."""
        kind = event.get("type")
        index = event.get("index")
        if not isinstance(index, int):
            return ""
        if kind == "content_block_start":
            block = as_object(event.get("content_block")) or {}
            is_tool = block.get("type") == "tool_use" and block.get("name") == self.tool
            self._blocks[index] = _Block(streams_input=is_tool)
            return ""
        if kind != "content_block_delta":
            return ""
        delta = as_object(event.get("delta")) or {}
        match delta.get("type"):
            case "text_delta":
                return as_str(delta.get("text")) or ""
            case "input_json_delta":
                block = self._blocks.get(index)
                if block is None or not block.streams_input:
                    return ""
                block.partial_json += as_str(delta.get("partial_json")) or ""
                decoded = partial_string_field(block.partial_json, self.field_name)
                if decoded is None or len(decoded) <= block.shown:
                    return ""
                added = decoded[block.shown :]
                block.shown = len(decoded)
                return added
            case _:
                return ""


def partial_string_field(partial_json: str, name: str) -> str | None:
    """The decoded prefix of top-level string field `name` in a JSON object that is
    still being written, or None until its opening quote has arrived."""
    match = re.search(r'"' + re.escape(name) + r'"\s*:\s*"', partial_json)
    if match is None:
        return None
    out: list[str] = []
    text = partial_json
    i = match.end()
    while i < len(text):
        char = text[i]
        if char == '"':
            break
        if char != "\\":
            out.append(char)
            i += 1
            continue
        if i + 1 >= len(text):
            break  # the escape's second half hasn't arrived
        code = text[i + 1]
        if code != "u":
            out.append(_ESCAPES.get(code, code))
            i += 2
            continue
        decoded = _unicode_escape(text, i)
        if decoded is None:
            break
        char, i = decoded
        out.append(char)
    return "".join(out)


def _unicode_escape(text: str, start: int) -> tuple[str, int] | None:
    """Decode `\\uXXXX` (and a following low surrogate) at `start`; None if incomplete."""
    hex_digits = text[start + 2 : start + 6]
    if len(hex_digits) < 4:
        return None
    value = int(hex_digits, 16)
    end = start + 6
    if 0xD800 <= value < 0xDC00:
        low = text[end : end + 6]
        if len(low) < 6:
            return None  # wait for the low surrogate rather than emit half a character
        if low.startswith("\\u"):
            low_value = int(low[2:], 16)
            if 0xDC00 <= low_value < 0xE000:
                combined = 0x10000 + ((value - 0xD800) << 10) + (low_value - 0xDC00)
                return chr(combined), end + 6
        return "�", end
    if 0xDC00 <= value < 0xE000:
        return "�", end
    return chr(value), end


__all__ = ["SEND_MESSAGE", "TEXT_FIELD", "OutgoingText", "partial_string_field"]
