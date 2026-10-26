"""Test doubles for `agents.runtime.run_turn`: an in-memory transcript store with the
SQL store's append rules, a checkpointed model over `ScriptedModel` (memoized on
(turn_id, step_idx), as `call_model_checkpointed` is), a recording executor, and the
API's tool_use/tool_result pairing rule, which `llm/contract.py` doesn't check."""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Final, get_args
from uuid import UUID, uuid5

from pydantic import JsonValue

from harmony.agents.runtime import (
    CardRaised,
    ConsultOpened,
    MessageSent,
    PerformativeName,
    ToolCall,
    ToolExecution,
    TurnContext,
    TurnRole,
)
from harmony.agents.transcript import (
    ConversationTranscript,
    RepSideTranscript,
    TranscriptConflict,
    TranscriptRef,
    WireMessage,
)
from harmony.llm import ModelRequest, StreamEventCallback
from harmony.llm.checkpoint import CheckpointedCall
from harmony.llm.scripted import (
    CassetteEntry,
    CassetteKey,
    CassetteMatch,
    ScriptedBlock,
    ScriptedModel,
    TextBlock,
    ToolUseBlock,
)
from harmony.llm.wire import JsonObject, as_str, canonical_json, objects

SCENARIO: Final = "e1-runtime"
ORG: Final = "northlake"
THREAD: Final = "nl-hl"
NAMESPACE: Final = UUID("6f0f1f52-55b4-4bb5-9e7f-1f6a0c1e0e01")


def tool(name: str) -> JsonObject:
    return {"name": name, "description": f"The {name} tool.", "input_schema": {"type": "object"}}


REP_TOOL_DEFS: Final = [tool(name) for name in sorted(get_args(PerformativeName))]
INTERNAL_TOOL_DEFS: Final = [tool(name) for name in ("search_network", "send_network_request")]


def use(name: str, call_id: str, **tool_input: JsonValue) -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", name=name, id=call_id, input=dict(tool_input))


def say(text: str) -> TextBlock:
    return TextBlock(type="text", text=text)


def cassette(
    steps: Mapping[tuple[int, int], Sequence[ScriptedBlock]],
    *,
    role: TurnRole = "representative",
    stop: Mapping[tuple[int, int], tuple[str, JsonObject | None]] | None = None,
) -> dict[CassetteKey, CassetteEntry]:
    """Entries keyed (turn, step); `stop` overrides a step's stop reason (and details)."""
    entries: dict[CassetteKey, CassetteEntry] = {}
    for (turn, step), content in steps.items():
        reason, details = (stop or {}).get((turn, step), (None, None))
        entries[(SCENARIO, ORG, role, THREAD, turn, step)] = CassetteEntry.model_validate(
            {
                "match": CassetteMatch(org=ORG, role=role, thread=THREAD, turn=turn, step=step),
                "content": list(content),
                "stop_reason": reason,
                "stop_details": details,
            }
        )
    return entries


class MemoryTranscripts:
    """`TranscriptStore` in memory, with `SqlTranscriptStore`'s append rules."""

    def __init__(self) -> None:
        self.rows: dict[str, list[WireMessage]] = {}
        self.authors: dict[str, list[UUID | None]] = {}
        self.appends = 0

    @staticmethod
    def _key(ref: TranscriptRef) -> str:
        if isinstance(ref, ConversationTranscript):
            return f"c:{ref.conversation_id}"
        return f"r:{ref.thread_id}:{ref.side}"

    def messages(self, ref: TranscriptRef) -> list[WireMessage]:
        return list(self.rows.get(self._key(ref), []))

    async def load(self, ref: TranscriptRef) -> list[WireMessage]:
        return self.messages(ref)

    async def append(
        self,
        ref: TranscriptRef,
        messages: Sequence[WireMessage],
        *,
        turn_id: UUID,
        author_member_id: UUID | None = None,
        position: int | None = None,
    ) -> list[int]:
        self.appends += 1
        rows = self.rows.setdefault(self._key(ref), [])
        authors = self.authors.setdefault(self._key(ref), [])
        own = [i for i, row in enumerate(rows) if row.turn_id == turn_id]
        start = len(own) if position is None else position
        if start > len(own):
            raise TranscriptConflict(f"can't append at {start} after {len(own)}")
        seqs: list[int] = []
        added = 0
        for offset, message in enumerate(messages):
            at = start + offset
            if at < len(own):
                if not rows[own[at]].same_bytes(message):
                    raise TranscriptConflict(f"different message at position {at}")
                seqs.append(own[at] + 1)
                continue
            if own and added == 0 and own[-1] != len(rows) - 1:
                raise TranscriptConflict("the turn is no longer the tail")
            rows.append(message.model_copy(update={"turn_id": turn_id}))
            authors.append(author_member_id if message.role == "user" else None)
            added += 1
            seqs.append(len(rows))
        return seqs


def tool_pairing(messages: Sequence[Mapping[str, JsonValue]]) -> list[str]:
    """Where `messages` breaks the API's tool-use rule, with system messages skipped and
    consecutive user messages read as one turn (as the API merges them): every call of a
    response is answered by exactly one result in the next user turn, the results lead
    that turn, and no other user turn carries a result."""
    turns: list[tuple[str, list[JsonObject]]] = []
    for message in messages:
        role = str(message.get("role"))
        if role == "system":
            continue
        blocks = objects(message.get("content"))
        if turns and role == "user" and turns[-1][0] == "user":
            turns[-1][1].extend(blocks)
        else:
            turns.append((role, list(blocks)))
    problems: list[str] = []
    calls: list[str] = []
    for index, (role, blocks) in enumerate(turns):
        if role == "assistant":
            if calls:
                problems.append(f"turn {index}: calls {calls} were never answered")
            calls = [as_str(b.get("id")) or "" for b in blocks if b.get("type") == "tool_use"]
            continue
        results = [
            as_str(b.get("tool_use_id")) or "" for b in blocks if b.get("type") == "tool_result"
        ]
        leading = list(itertools.takewhile(lambda b: b.get("type") == "tool_result", blocks))
        if len(leading) != len(results):
            problems.append(f"turn {index}: a tool result comes after other content")
        if sorted(results) != sorted(calls):
            problems.append(f"turn {index}: results {results} answer calls {calls}")
        calls = []
    return problems


class Checkpointed:
    """`CheckpointedModel` over a `ScriptedModel`: one real call per (turn_id, step),
    reused afterwards, like `call_model_checkpointed`. Calls listed in `fail_once` raise
    `RuntimeError` the first time, before the model sees them."""

    def __init__(self, model: ScriptedModel, *, fail_once: Iterable[tuple[UUID, int]] = ()) -> None:
        self.model = model
        self.stored: dict[tuple[UUID, int], CheckpointedCall] = {}
        self.reused = 0
        self.fail_once = set(fail_once)

    @property
    def requests(self) -> list[ModelRequest]:
        return self.model.requests

    async def call(
        self, request: ModelRequest, *, on_stream_event: StreamEventCallback | None = None
    ) -> CheckpointedCall:
        key = (request.key.turn_id, request.key.step_idx)
        if key in self.fail_once:
            self.fail_once.discard(key)
            raise RuntimeError(f"model call {key} failed")
        stored = self.stored.get(key)
        if stored is not None:
            self.reused += 1
            return dataclasses.replace(stored, reused=True)
        result = await self.model.call(request, on_stream_event=on_stream_event)
        call = CheckpointedCall(
            turn_id=request.key.turn_id,
            step_idx=request.key.step_idx,
            world_id=request.key.world_id,
            request_text=canonical_json(request.body),
            response_text=result.response_text,
            served_model=result.served_model,
            usage=dict(result.usage),
            cost_usd=Decimal(0),
            attempt=request.key.attempt,
            reused=False,
        )
        self.stored[key] = call
        return call


class Executor:
    """Records every call. For a representative, `send_message` sends and the turn goes
    on, `consult` ends it with a pending result and `close` ends it ("Closed."); a bare
    text reply is a send while the turn has sent nothing. `cards` names tools that raise
    a card; anything else returns "<name> ok". `fail_once` names tools whose first
    execution raises. One executor serves one turn, as in the activities."""

    def __init__(
        self,
        *,
        role: TurnRole = "representative",
        cards: Iterable[str] = (),
        fail_once: Iterable[str] = (),
    ) -> None:
        self.role = role
        self.cards = frozenset(cards)
        self.fail_once = set(fail_once)
        self.executed: list[tuple[str, int]] = []
        self.replies: list[tuple[str, int]] = []
        self.sent = False

    async def execute(self, call: ToolCall, *, step_idx: int) -> ToolExecution:
        self.executed.append((call.name, step_idx))
        if call.name in self.fail_once:
            self.fail_once.discard(call.name)
            raise RuntimeError(f"{call.name} failed")
        if call.name in self.cards:
            card = CardRaised(
                card_id=uuid5(NAMESPACE, call.id), card_kind="outreach", tool_use_id=call.id
            )
            return ToolExecution(ends_turn=True, effect=card)
        if self.role != "representative":
            return ToolExecution(result=f"{call.name} ok")
        message_id = uuid5(NAMESPACE, call.id)
        match call.name:
            case "consult":
                return ToolExecution(ends_turn=True, effect=ConsultOpened(consult_id=message_id))
            case "close":
                closed = MessageSent(message_id=message_id, message_kind="close")
                return ToolExecution(ends_turn=True, owed_result="Closed.", effect=closed)
            case "send_message":
                self.sent = True
                sent = MessageSent(message_id=message_id, message_kind="message")
                return ToolExecution(result="Sent.", effect=sent)
            case _:
                return ToolExecution(result=f"{call.name} ok")

    async def text_reply(self, text: str, *, step_idx: int) -> ToolExecution | None:
        self.replies.append((text, step_idx))
        if self.role != "representative" or self.sent:
            return None
        self.sent = True
        sent = MessageSent(message_id=uuid5(NAMESPACE, f"reply/{step_idx}"), message_kind="message")
        return ToolExecution(result="Sent.", effect=sent)


def turn_context(
    *,
    world_id: UUID,
    turn: int,
    turn_id: UUID,
    opening: list[JsonObject],
    role: TurnRole = "representative",
    transcript: TranscriptRef | None = None,
    notices: str | None = None,
    first_step: int = 0,
    max_steps: int = 8,
    author: UUID | None = None,
    system: str = "You are Claude for Northlake.",
) -> TurnContext:
    tools = REP_TOOL_DEFS if role == "representative" else INTERNAL_TOOL_DEFS
    return TurnContext(
        world_id=world_id,
        scenario=SCENARIO,
        role=role,
        org_id=uuid5(NAMESPACE, "org"),
        org_slug=ORG,
        tier="featured",
        turn_id=turn_id,
        turn=turn,
        cassette_thread=THREAD,
        transcript=transcript
        or RepSideTranscript(
            world_id=world_id, thread_id=uuid5(NAMESPACE, "thread"), side="responder"
        ),
        system=system,
        tools=list[dict[str, JsonValue]](tools),
        opening=list[dict[str, JsonValue]](opening),
        opening_author_member_id=author,
        notices=notices,
        first_step=first_step,
        max_steps=max_steps,
    )
