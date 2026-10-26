"""`ScriptedModel`: plays cassettes instead of calling the API, at $0.

A cassette is a YAML file of canned responses keyed by
(scenario, org, role, thread, turn, step):

    scenario: harmony-demo
    entries:
      - match: {org: northlake, role: representative, thread: nl-hl, turn: 1, step: 0}
        content:
          - {type: thinking}                      # empty text, placeholder signature
          - {type: text, text: "Happy to help."}
          - {type: tool_use, name: send_message, input: {text: "..."}}
        # optional: stop_reason (default tool_use if any tool_use block, else
        # end_turn), stop_details, served_model (default: the requested model)

`thread: "*"` matches any thread (for an org that plays the same script everywhere,
like the adversary). The model streams the response as the API would (message_start,
content blocks with deltas, message_delta, message_stop), reports synthetic usage
that `FreeSpend` prices at $0, records every request, and writes no ledger lines.

With `strict_contract` (the default) each request is checked like the real API would
check it, plus the byte-prefix rule (`llm/contract.py`); a violation raises
`BadRequest`, so contract bugs fail tests for free.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Hashable, Iterable, Mapping
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from harmony.llm import (
    CallKey,
    HeartbeatCallback,
    ModelRequest,
    ModelResult,
    ModelRole,
    StreamEventCallback,
)
from harmony.llm.contract import check_prefix, check_request, conversation_key
from harmony.llm.errors import BadRequest
from harmony.llm.wire import JsonObject, as_str, canonical_json

ANY_THREAD = "*"
SCRIPTED_SIGNATURE_PREFIX = "scripted-signature:"

StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "pause_turn"]
type CassetteKey = tuple[str, str, str, str | None, int, int]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ThinkingBlock(_Strict):
    type: Literal["thinking"]
    thinking: str = ""


class TextBlock(_Strict):
    type: Literal["text"]
    text: str


class ToolUseBlock(_Strict):
    type: Literal["tool_use"]
    name: str
    input: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    id: str | None = None


ScriptedBlock = Annotated[ThinkingBlock | TextBlock | ToolUseBlock, Field(discriminator="type")]


class CassetteMatch(_Strict):
    org: str
    role: ModelRole
    thread: str | None = None
    turn: int = Field(ge=1)
    step: int = Field(default=0, ge=0)


class CassetteEntry(_Strict):
    match: CassetteMatch
    content: list[ScriptedBlock]
    stop_reason: StopReason | None = None
    stop_details: dict[str, JsonValue] | None = None
    served_model: str | None = None


class Cassette(_Strict):
    scenario: str
    entries: list[CassetteEntry]


def load_cassettes(*paths: Path) -> dict[CassetteKey, CassetteEntry]:
    """Every entry in the given files (directories: their *.yaml and *.yml, sorted)."""
    entries: dict[CassetteKey, CassetteEntry] = {}
    sources: dict[CassetteKey, Path] = {}
    for path in _cassette_files(paths):
        try:
            raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
            cassette = Cassette.model_validate(raw)
        except (yaml.YAMLError, ValidationError) as exc:
            raise ValueError(f"invalid cassette {path}: {exc}") from exc
        for entry in cassette.entries:
            m = entry.match
            key: CassetteKey = (cassette.scenario, m.org, m.role, m.thread, m.turn, m.step)
            if key in entries:
                raise ValueError(f"cassette key {key} is defined in {sources[key]} and {path}")
            entries[key] = entry
            sources[key] = path
    return entries


def _cassette_files(paths: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files += sorted(p for p in path.iterdir() if p.suffix in (".yaml", ".yml"))
        else:
            files.append(path)
    return files


class ScriptedModel:
    def __init__(
        self,
        entries: Mapping[CassetteKey, CassetteEntry],
        *,
        chunk_chars: int = 24,
        event_delay_s: float = 0.0,
        strict_contract: bool = True,
    ) -> None:
        if chunk_chars < 1:
            raise ValueError("chunk_chars must be at least 1")
        self._entries = dict(entries)
        self._chunk = chunk_chars
        self._delay = event_delay_s
        self._strict = strict_contract
        self._last: dict[Hashable, ModelRequest] = {}
        self.requests: list[ModelRequest] = []

    @classmethod
    def from_paths(
        cls,
        *paths: Path,
        chunk_chars: int = 24,
        event_delay_s: float = 0.0,
        strict_contract: bool = True,
    ) -> ScriptedModel:
        return cls(
            load_cassettes(*paths),
            chunk_chars=chunk_chars,
            event_delay_s=event_delay_s,
            strict_contract=strict_contract,
        )

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def forget_conversation(self, request: ModelRequest) -> None:
        """Start the prefix rule afresh for this request's conversation: after a
        request whose notices weren't stored (a refused turn), the next one can't
        extend it byte for byte."""
        conversation = conversation_key(request)
        if conversation is not None:
            self._last.pop(conversation, None)

    async def call(
        self,
        request: ModelRequest,
        *,
        on_stream_event: StreamEventCallback | None = None,
        heartbeat: HeartbeatCallback | None = None,
    ) -> ModelResult:
        self.requests.append(request)
        if self._strict:
            self._enforce_contract(request)
        entry = self._lookup(request.key)
        requested_model = as_str(request.body.get("model")) or "scripted"
        served_model = entry.served_model or requested_model
        content = [self._block(request.key, i, block) for i, block in enumerate(entry.content)]
        stop_reason: StopReason = entry.stop_reason or (
            "tool_use" if any(b.type == "tool_use" for b in entry.content) else "end_turn"
        )
        usage: JsonObject = {
            "input_tokens": max(1, len(canonical_json(request.body).encode()) // 4),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": max(1, len(canonical_json(list[JsonValue](content)).encode()) // 4),
        }
        message: JsonObject = {
            "id": "msg_" + _token(request.key, -1),
            "type": "message",
            "role": "assistant",
            "model": served_model,
            "content": list[JsonValue](content),
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": usage,
        }
        if entry.stop_details is not None:
            message["stop_details"] = entry.stop_details
        for event in self._events(message, content, usage):
            if self._delay:
                await asyncio.sleep(self._delay)
            if heartbeat is not None:
                heartbeat()
            if on_stream_event is not None:
                await on_stream_event(event)
        category = (entry.stop_details or {}).get("category") if stop_reason == "refusal" else None
        return ModelResult(
            response_text=canonical_json(message),
            served_model=served_model,
            stop_reason=stop_reason,
            usage=usage,
            refusal_category=category if isinstance(category, str) else None,
        )

    def _enforce_contract(self, request: ModelRequest) -> None:
        violations = check_request(request)
        conversation = conversation_key(request)
        if conversation is not None:
            previous = self._last.get(conversation)
            if previous is not None:
                violations += check_prefix(previous, request)
            self._last[conversation] = request
        if violations:
            raise BadRequest(
                "scripted API rejected the request: " + "; ".join(map(str, violations))
            )

    def _lookup(self, key: CallKey) -> CassetteEntry:
        exact: CassetteKey = (key.scenario, key.org, key.role, key.thread, key.turn, key.step_idx)
        wildcard: CassetteKey = (
            key.scenario,
            key.org,
            key.role,
            ANY_THREAD,
            key.turn,
            key.step_idx,
        )
        entry = self._entries.get(exact) or self._entries.get(wildcard)
        if entry is None:
            raise BadRequest(f"no cassette entry for {exact}")
        return entry

    def _block(
        self, key: CallKey, index: int, block: ThinkingBlock | TextBlock | ToolUseBlock
    ) -> JsonObject:
        if isinstance(block, ThinkingBlock):
            return {
                "type": "thinking",
                "thinking": block.thinking,
                "signature": SCRIPTED_SIGNATURE_PREFIX + _token(key, index),
            }
        if isinstance(block, TextBlock):
            return {"type": "text", "text": block.text}
        return {
            "type": "tool_use",
            "id": block.id or "toolu_" + _token(key, index),
            "name": block.name,
            "input": dict(block.input),
        }

    def _events(
        self, message: JsonObject, content: list[JsonObject], usage: JsonObject
    ) -> Iterable[JsonObject]:
        start_usage: JsonObject = {**usage, "output_tokens": 1}
        skeleton: JsonObject = {
            **message,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": start_usage,
        }
        skeleton.pop("stop_details", None)
        yield {"type": "message_start", "message": skeleton}
        for index, block in enumerate(content):
            yield from self._block_events(index, block)
        delta: JsonObject = {"stop_reason": message["stop_reason"], "stop_sequence": None}
        if "stop_details" in message:
            delta["stop_details"] = message["stop_details"]
        yield {
            "type": "message_delta",
            "delta": delta,
            "usage": {"output_tokens": usage["output_tokens"]},
        }
        yield {"type": "message_stop"}

    def _block_events(self, index: int, block: JsonObject) -> Iterable[JsonObject]:
        kind = block["type"]
        if kind == "thinking":
            yield _start(index, {"type": "thinking", "thinking": "", "signature": ""})
            for piece in self._chunks(as_str(block["thinking"]) or ""):
                yield _delta(index, {"type": "thinking_delta", "thinking": piece})
            yield _delta(index, {"type": "signature_delta", "signature": block["signature"]})
        elif kind == "text":
            yield _start(index, {"type": "text", "text": ""})
            for piece in self._chunks(as_str(block["text"]) or ""):
                yield _delta(index, {"type": "text_delta", "text": piece})
        else:
            yield _start(
                index, {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            )
            for piece in self._chunks(canonical_json(block["input"])):
                yield _delta(index, {"type": "input_json_delta", "partial_json": piece})
        yield {"type": "content_block_stop", "index": index}

    def _chunks(self, text: str) -> list[str]:
        return [text[i : i + self._chunk] for i in range(0, len(text), self._chunk)]


def _start(index: int, block: JsonObject) -> JsonObject:
    return {"type": "content_block_start", "index": index, "content_block": block}


def _delta(index: int, delta: JsonObject) -> JsonObject:
    return {"type": "content_block_delta", "index": index, "delta": delta}


def _token(key: CallKey, index: int) -> str:
    # Deterministic per (turn, step, block): a retried call yields the same ids.
    seed = f"{key.turn_id}:{key.step_idx}:{index}".encode()
    return hashlib.sha256(seed).hexdigest()[:24]


__all__ = [
    "ANY_THREAD",
    "SCRIPTED_SIGNATURE_PREFIX",
    "Cassette",
    "CassetteEntry",
    "CassetteKey",
    "CassetteMatch",
    "ScriptedModel",
    "load_cassettes",
]
