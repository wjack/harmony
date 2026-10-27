"""The checkpointed tool loop every agent turn runs (§5 Turn protocol, Checkpointed
activities). One activity runs one turn's whole loop.

`run_turn(ctx, executor, model, transcript=...)`:
1. The turn's first call sends `ctx.opening` as a user message (tool results owed from
   the side's previous turn, then new input such as the counterparty's delimited
   message or the member's text; a turn resumed after a card opens with the card's
   tool result), followed by `ctx.notices` as one `role: "system"` message. Later
   calls send the previous step's tool results. A new turn whose transcript ends on a
   response with unanswered tool calls (an earlier turn failed between storing that
   response and its results) opens with an error result for each (`INTERRUPTED`), so
   no request ever carries a `tool_use` without its `tool_result`.
2. Steps run from `ctx.first_step` while `step < ctx.max_steps` (8). The turn's first
   call is always made, so a turn resumed after a card raised on its last step still
   stores and answers the card's result. Each step builds the request with
   `llm.requests.build_request` from the frozen system prompt and tools plus the stored
   transcript, and calls `model.call(request)`, checkpointed on `(turn_id, step_idx)`.
   A refusal is handled before any content is read; a response that didn't end on its
   own (`max_tokens`, `pause_turn`, ...) raises `TurnIncomplete`, and so does an empty
   one, or a first call's `end_turn` that says nothing (thinking alone: `says_nothing`).
   The exceptions are an `end_turn` answering this turn's own tool results, and a first
   call whose opening asks for no answer (`TurnContext.quiet_opening`): that ends the
   turn (the model had nothing to add after "Sent.").
3. Nothing is written ahead of a call (§2.2). Once a call is stored, its new input
   messages and the response are appended to the transcript together, in one
   transaction, taken from the checkpointed request's own bytes: a retried attempt
   that reuses a stored call stores exactly what that call sent. A refused call, an
   incomplete response and a call that raised all consume their input: its user
   messages are stored without the notices (a stored system message must be followed
   by a response) and without a response, so the transcript ends on a user message
   and the next request stays legal.
4. Tool calls run in order through `executor`. Non-performative results go back as one
   `user` message of `tool_result` blocks and the loop continues. A performative
   (`ToolExecution.ends_turn`) stops the turn: its `tool_result` is owed to the side's
   next turn (`TurnOutcome.owed`), and calls after it in the same response are not
   run (they get an error result; after a request sent without review, a further
   `send_network_request` is told to call again now, `NOT_RUN_YET`). A card
   (`CardRaised`) stops the turn as `awaiting_card`; the workflow resumes it with the
   same turn_id at `next_step`.
   Results already known when a turn stops (other calls of the stopping step, or the
   last step's results at `max_steps`) are stored right away, so the next opening
   only has to carry the owed one.
5. Representatives: `disable_parallel_tool_use` is set by the request builder. A turn
   may send several messages (`send_message` doesn't stop it) and ends when the model
   stops calling tools (R§5). A bare text reply to the turn's opening (its first call)
   goes to `executor.text_reply`, which maps it to `send_message` only while the turn
   has sent nothing and had something to answer (`TurnContext.bare_text_sends`); other
   text is the model's own, text after the turn's own tool results included (it
   answers them: a refused `close`, say, not the counterparty). So text is shown live
   only when it may be sent: a `send_message` text streams as it is written, and text
   blocks only once the first call's response has ended without a tool call, in a turn
   that may send bare text. Text in the same response as a tool call is never sent or
   shown live (it stays in the stored transcript).

Retries. A turn's stored messages are the transcript's tail, tagged with its turn_id
(`agents.transcript`). A rerun of the same turn picks up where the stored messages
end: the last stored response is processed again (executors are idempotent), and the
next call reuses its checkpoint. A call whose error result is already stored keeps it
and isn't run again: an error stores no decision to find again, and a guard that reads
live state (the chat's send mode, its open conversations) could now decide otherwise,
acting on a call the model was already told failed. Only the run's first call carries
the notices; when that call is consumed without a response (refusal, incomplete, failed,
or an empty answer that ends the turn) the notices are not stored, and
`context.flush_notices` delivers them again at the side's next turn.

Callers write nothing for the turn themselves: `run_turn` stores the opening (with
`ctx.opening_author_member_id` on a typed chat message).

Owner: R-agents (`PerformativeName`, `TurnStop` and the `TurnEffect` union are the
contract's).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Final, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.agents.models import ScreenedCounts
from harmony.agents.transcript import (
    TranscriptConflict,
    TranscriptRef,
    TranscriptStore,
    WireMessage,
)
from harmony.db.models.choices import CardKind, ThreadMessageKind
from harmony.errors import HarmonyError
from harmony.llm import (
    CallKey,
    HeartbeatCallback,
    ModelClient,
    ModelRequest,
    StreamEventCallback,
)
from harmony.llm.budget import SpendGuard
from harmony.llm.checkpoint import CheckpointedCall, call_model_checkpointed
from harmony.llm.outgoing import OutgoingText
from harmony.llm.requests import Effort, PrefixMismatch, Tier, build_request
from harmony.llm.wire import JsonObject, as_object, as_str, canonical_json, objects

MAX_STEPS: Final = 8

# The representative tools that can stop a turn before the model does (REVISION §5):
# `consult` until its answer is released, `close` for good, and `send_message` when *Ask*
# holds the message for a teammate. Otherwise a turn ends when the model stops calling
# tools.
PerformativeName = Literal["send_message", "consult", "close"]
_SEND_NETWORK_REQUEST: Final = "send_network_request"
# A response is processed only if it ended on its own: `max_tokens` cuts a tool input
# mid-string, and `pause_turn` is unfinished.
COMPLETE_STOP_REASONS: Final = frozenset({"tool_use", "end_turn"})
# The result of a call that came after the call that ended the turn.
NOT_RUN: Final = "Not run: {name} ended your turn. Call it again in a later turn if needed."
# The result of a `send_network_request` after one sent without review in the same
# response: the turn resumes as soon as that one is sent, so it may be called again now.
NOT_RUN_YET: Final = (
    "Not run yet: requests go out one at a time. Call it again now if you still want to send it."
)
# The result a new turn supplies for a call whose turn failed before storing its result.
INTERRUPTED: Final = (
    "No result was recorded: the turn that made this call was interrupted. It may have "
    "taken effect; check before you repeat it."
)
# After a mid-output server-side fallback, blocks before the last `fallback` block are
# left out when echoing (§5 Refusals and fallbacks).
FALLBACK_BLOCK: Final = "fallback"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TurnIncomplete(HarmonyError):
    """The model's response didn't end on its own, or was empty where the turn still
    owed an answer: the call's input is stored, the response isn't. Retrying replays
    the same checkpointed response, so callers treat it as final."""

    def __init__(self, message: str, *, stop_reason: str | None) -> None:
        super().__init__(message)
        self.stop_reason = stop_reason


# --- Tool calls and their effects ----------------------------------------------------


class ToolCall(_Model):
    """One `tool_use` block from a response."""

    id: str
    name: str
    input: dict[str, JsonValue]


class MessageSent(_Model):
    """A `thread_messages` row was committed: send_message or close."""

    kind: Literal["message_sent"] = "message_sent"
    message_id: UUID
    message_kind: ThreadMessageKind


class ConsultOpened(_Model):
    """A consult was opened (or an identical open one reused, `deduplicated`); the
    thread is `awaiting_principal`."""

    kind: Literal["consult_opened"] = "consult_opened"
    consult_id: UUID
    deduplicated: bool = False


class MessageHeld(_Model):
    """In *Ask*, a send_message was held for a teammate instead of sent: an
    `outbound_review` escalation. The thread is `awaiting_principal`; the call's result
    is owed once a person resolves it (`escalations.held_message_id` is the id an
    approval sends the message under)."""

    kind: Literal["message_held"] = "message_held"
    escalation_id: UUID


class CardRaised(_Model):
    """Internal Claude raised an approval or confirmation card; the turn pauses."""

    kind: Literal["card_raised"] = "card_raised"
    card_id: UUID
    card_kind: CardKind
    tool_use_id: str
    # The member's chat sends without confirmation: the workflow approves it itself.
    auto: bool = False


class SearchRan(_Model):
    """`search_network` screened the network (informational; the turn continues)."""

    kind: Literal["search_ran"] = "search_ran"
    search_id: UUID
    screened: ScreenedCounts


TurnEffect = Annotated[
    MessageSent | ConsultOpened | MessageHeld | CardRaised | SearchRan,
    Field(discriminator="kind"),
]


class ToolExecution(_Model):
    """What executing one tool call produced.

    - `result` is the `tool_result` content appended now (non-performative calls, and
      any call that failed validation: `is_error`, the model may try again).
    - `ends_turn` marks a performative or a card. Its `tool_result` isn't appended now:
      `owed_result` is the text the side's next turn opens with when it is already known
      ("Sent."), or None when it arrives later (a consult answer, a card resolution).
    """

    result: str | None = None
    is_error: bool = False
    ends_turn: bool = False
    owed_result: str | None = None
    effect: TurnEffect | None = None


class OwedResult(_Model):
    """A `tool_result` a turn ended without: the next turn's opening must carry it."""

    tool_use_id: str
    content: str | None = Field(
        description="The result text, or None while it is pending (consult, card)."
    )
    is_error: bool = False


# --- The turn ------------------------------------------------------------------------

TurnRole = Literal["internal", "representative"]
TurnStop = Literal["performative", "end_turn", "awaiting_card", "max_steps", "refusal"]


class TurnContext(_Model):
    """Everything one turn needs that isn't loaded from its transcript.

    `system` and `tools` are the frozen bytes for a representative (`rep_sessions`) and
    the rendered internal prompt for a conversation; the request builder must receive
    them unchanged on every call of a (thread, side) or chat (§2.2).
    """

    world_id: UUID
    scenario: str
    role: TurnRole
    org_id: UUID
    org_slug: str
    tier: Tier
    turn_id: UUID
    turn: int = Field(ge=1, description="The cassette turn number (`agents.keys`).")
    cassette_thread: str = Field(description="The cassette thread key (`agents.keys`).")
    transcript: TranscriptRef
    system: str
    tools: list[dict[str, JsonValue]]
    opening: list[dict[str, JsonValue]] = Field(
        min_length=1,
        description="Content blocks of the user message that opens this turn: owed "
        "tool_result blocks first, then new input. A turn resumed after a card opens "
        "with the card's tool_result alone. Never repeat a result already stored; "
        "results the transcript still lacks are supplied as `INTERRUPTED` errors.",
    )
    opening_author_member_id: UUID | None = Field(
        default=None,
        description="The member who typed the opening (a chat message), stored as its "
        "author; None for code-written openings.",
    )
    notices: str | None = Field(
        default=None,
        description="Merged pending notices (`context.flush_notices`), sent as one "
        "system message after the opening on the turn's first call.",
    )
    first_step: int = Field(default=0, ge=0)
    max_steps: int = Field(default=MAX_STEPS, ge=1)
    max_tokens: int = Field(default=8192, ge=1)
    stream_to_ui: bool = False
    # False when nothing a bare reply says can be sent (a code-written fallback or a
    # declined held message opens the turn, or the thread has ended): then a text-only
    # response stays private and isn't streamed.
    bare_text_sends: bool = True
    # True when the opening asks for no answer: a representative's code-written "nothing
    # new" or its held message declined without a reason, or a resumed turn's own card
    # result once its request went out. An empty `end_turn` on the first call then ends
    # the turn, as one after its own tool results does (3.13 E1b).
    quiet_opening: bool = False
    effort: Effort | None = None
    model_override: str | None = None
    prefix_mismatch: PrefixMismatch | None = None
    attempt: int = Field(default=1, ge=1, description="The Temporal activity attempt.")


class TurnOutcome(_Model):
    turn_id: UUID
    stop: TurnStop
    # The step the next call of this turn would use (for resuming after a card).
    next_step: int = Field(ge=0)
    # Model calls made or reused by this run of the turn.
    calls: int = Field(ge=0)
    performative: PerformativeName | None = None
    effects: list[TurnEffect] = []
    owed: OwedResult | None = None
    refusal_category: str | None = None
    # Internal Claude's last text block, for logs and tests.
    final_text: str | None = None


# --- Protocols -----------------------------------------------------------------------


class ToolExecutor(Protocol):
    """Runs one role's tools for one turn (`tools.internal`, `tools.rep`).

    Executors validate input against the tool's input model, perform the side effect
    with IDs `ids.side_effect_id(turn_id, step_idx, tool_use_id)`, and return errors as
    `ToolExecution(result=..., is_error=True)` rather than raising, so the model sees
    them. They raise only for infrastructure failures (Temporal retries those).
    """

    async def execute(self, call: ToolCall, *, step_idx: int) -> ToolExecution: ...

    async def text_reply(self, text: str, *, step_idx: int) -> ToolExecution | None:
        """A response with text and no tool call. Representatives map it to
        `send_message` (§5); internal Claude returns None (a normal reply, end of turn)."""
        ...


class CheckpointedModel(Protocol):
    """The model as `run_turn` sees it: one checkpointed call per (turn_id, step_idx)."""

    async def call(
        self,
        request: ModelRequest,
        *,
        on_stream_event: StreamEventCallback | None = None,
    ) -> CheckpointedCall: ...


# The hosted budget keys a call reserves against (`budget_pg.hosted_budget_keys`).
BudgetKeys = Callable[[], Awaitable[Sequence[str]]]


@dataclass(frozen=True, slots=True)
class CheckpointedClient:
    """`CheckpointedModel` over `call_model_checkpointed` (activities build one per turn
    with `heartbeat=activity.heartbeat`).

    `budget_keys` is asked again for every call: the day caps are keyed on the UTC
    date, and a turn or answerer running past midnight must charge its later calls to
    the new day."""

    sessionmaker: async_sessionmaker[AsyncSession]
    client: ModelClient
    guard: SpendGuard
    heartbeat: HeartbeatCallback | None = None
    budget_keys: BudgetKeys | None = None

    async def call(
        self,
        request: ModelRequest,
        *,
        on_stream_event: StreamEventCallback | None = None,
    ) -> CheckpointedCall:
        return await call_model_checkpointed(
            self.sessionmaker,
            self.client,
            self.guard,
            request,
            on_stream_event=on_stream_event,
            heartbeat=self.heartbeat,
            budget_keys=await self.budget_keys() if self.budget_keys is not None else (),
        )


# Receives the text that became visible with each stream event (`llm.outgoing`); the
# activity wires it to `events.stream.TurnStream.push`.
TextSink = Callable[[str], None]


# --- Wire helpers ---------------------------------------------------------------------


def text_block(text: str) -> JsonObject:
    return {"type": "text", "text": text}


def tool_result_block(tool_use_id: str, content: str, *, is_error: bool = False) -> JsonObject:
    block: JsonObject = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


def opening_blocks(owed: Sequence[OwedResult], texts: Sequence[str]) -> list[JsonObject]:
    """A turn's opening: the owed tool results first, as the API requires after a
    tool_use, then the new input. A result owed to no call (a held bare-text reply) is
    plain text. Raises `ValueError` for a result still pending."""
    blocks: list[JsonObject] = []
    unanswered: list[JsonObject] = []
    for result in owed:
        if result.content is None:
            raise ValueError(f"the result of {result.tool_use_id} is still pending")
        if not result.tool_use_id:
            # A held bare-text reply: there is no call for a tool result to answer.
            unanswered.append(text_block(result.content))
            continue
        blocks.append(
            tool_result_block(result.tool_use_id, result.content, is_error=result.is_error)
        )
    blocks += unanswered
    blocks += [text_block(text) for text in texts]
    return blocks


def echo_content(message: Mapping[str, JsonValue]) -> list[JsonObject]:
    """The response content as the next request must echo it: every block, except those
    before the last server-side `fallback` block."""
    blocks = objects(message.get("content"))
    last = max((i for i, b in enumerate(blocks) if b.get("type") == FALLBACK_BLOCK), default=0)
    return blocks[last:]


def says_nothing(content: Sequence[JsonObject]) -> bool:
    """Whether response content holds no tool call and no text that isn't blank.
    Thinking alone says nothing: its text is omitted unless the call streams it."""
    return (
        not any(block.get("type") == "tool_use" for block in content)
        and not _text_in(content).strip()
    )


def tool_calls(message: Mapping[str, JsonValue]) -> list[ToolCall]:
    """The `tool_use` blocks of a final message, in order."""
    return _calls_in(echo_content(message))


def _calls_in(content: Sequence[JsonObject]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for block in content:
        if block.get("type") != "tool_use":
            continue
        call_id, name = as_str(block.get("id")), as_str(block.get("name"))
        if call_id is None or name is None:
            raise TurnIncomplete("a tool_use block has no id or name", stop_reason=None)
        calls.append(ToolCall(id=call_id, name=name, input=as_object(block.get("input")) or {}))
    return calls


def _text_in(content: Sequence[JsonObject]) -> str:
    return "".join(as_str(b.get("text")) or "" for b in content if b.get("type") == "text")


def _result_ids(content: JsonValue) -> list[str]:
    return [
        as_str(block.get("tool_use_id")) or ""
        for block in objects(content)
        if block.get("type") == "tool_result"
    ]


def _answering(history: Sequence[WireMessage], opening: Sequence[JsonObject]) -> list[JsonValue]:
    """`opening`, led by an `INTERRUPTED` error result for every call of the last stored
    response that neither a stored message nor `opening` answers."""
    last = max((i for i, m in enumerate(history) if m.role == "assistant"), default=None)
    if last is None:
        return list[JsonValue](opening)
    answered = {i for message in history[last + 1 :] for i in _result_ids(message.content())}
    answered.update(_result_ids(list[JsonValue](opening)))
    calls = _calls_in(objects(history[last].content()))
    missing = [
        tool_result_block(call.id, INTERRUPTED, is_error=True)
        for call in calls
        if call.id not in answered
    ]
    return [*missing, *opening]


def _error_results(messages: Sequence[WireMessage]) -> dict[str, JsonObject]:
    """The error `tool_result` blocks in `messages`, by the call they answer."""
    return {
        as_str(block.get("tool_use_id")) or "": block
        for message in messages
        for block in objects(message.content())
        if block.get("type") == "tool_result" and block.get("is_error") is True
    }


def _user(content: list[JsonValue]) -> WireMessage:
    return WireMessage.of("user", content)


# --- The loop -------------------------------------------------------------------------


async def run_turn(
    ctx: TurnContext,
    executor: ToolExecutor,
    model: CheckpointedModel,
    *,
    transcript: TranscriptStore,
    on_text: TextSink | None = None,
) -> TurnOutcome:
    """Run one turn's tool loop (module docstring). Idempotent under activity retry:
    stored calls are reused, transcript appends and side effects are keyed on the turn."""
    history = await transcript.load(ctx.transcript)
    run = _TurnRun(ctx, executor, model, transcript, on_text, history=history)
    return await run.run()


@dataclass(slots=True)
class _TurnRun:
    ctx: TurnContext
    executor: ToolExecutor
    model: CheckpointedModel
    transcript: TranscriptStore
    on_text: TextSink | None
    history: list[WireMessage]
    # The content of the user message that opens the turn (`ctx.opening`, completed).
    opening: list[JsonValue] = field(default_factory=list[JsonValue])
    # The turn's stored messages so far; the next one goes at this position.
    position: int = 0
    # The step whose input is already stored (after the turn's last response), if any.
    stored_input_step: int | None = None
    calls: int = 0
    effects: list[TurnEffect] = field(default_factory=list[TurnEffect])
    streamed: bool = False

    async def run(self) -> TurnOutcome:
        ctx = self.ctx
        own = _own_tail(self.history, ctx.turn_id)
        self.position = len(own)
        # Completed against the transcript as it stood before the turn, so every
        # attempt of the turn builds the same bytes.
        before = self.history[: len(self.history) - len(own)]
        self.opening = _answering(before, ctx.opening) if ctx.first_step == 0 else list(ctx.opening)
        responses = [i for i, message in enumerate(own) if message.role == "assistant"]
        done = len(responses)
        if done < ctx.first_step:
            raise TranscriptConflict(
                f"turn {ctx.turn_id} resumes at step {ctx.first_step} but stored {done} steps"
            )
        after_last = responses[-1] + 1 if responses else 0
        if after_last < len(own):
            self.stored_input_step = done

        step = done
        content: list[JsonObject] | None = None
        results: list[JsonValue] = []
        refused: dict[str, JsonObject] = {}
        if done > ctx.first_step:
            # An earlier attempt of this run stored step `done - 1`; process it again.
            step = done - 1
            content = objects(own[responses[-1]].content())
            refused = _error_results(own[after_last:])
        while True:
            if content is None:
                # Only the turn's first call can come at `max_steps` or later: a resumed
                # turn must still store and answer the card's result.
                answer = await self._call(step, results)
                if isinstance(answer, TurnOutcome):
                    return answer
                content = answer
            stopped = await self._process(step, content, refused)
            if isinstance(stopped, TurnOutcome):
                return stopped
            results = stopped
            step += 1
            content = None
            refused = {}
            if step >= ctx.max_steps:
                await self._store_known(step, results)
                return self._outcome("max_steps", next_step=step)

    # --- One call ---------------------------------------------------------------------

    async def _call(self, step: int, results: list[JsonValue]) -> list[JsonObject] | TurnOutcome:
        ctx = self.ctx
        first = step == ctx.first_step
        author = ctx.opening_author_member_id if first else None
        fresh: list[WireMessage] = []
        if first:
            opening = _user(self.opening)
            if not self._stored_since_last_response(opening):
                fresh.append(opening)
        elif self.stored_input_step != step:
            fresh.append(_user(results))
        if first and ctx.notices:
            fresh.append(WireMessage.of("system", [text_block(ctx.notices)]))

        request = build_request(
            CallKey(
                world_id=ctx.world_id,
                scenario=ctx.scenario,
                org=ctx.org_slug,
                role=ctx.role,
                thread=ctx.cassette_thread,
                turn=ctx.turn,
                turn_id=ctx.turn_id,
                step_idx=step,
                attempt=ctx.attempt,
            ),
            tier=ctx.tier,
            system=ctx.system,
            messages=[message.as_json() for message in [*self.history, *fresh]],
            max_tokens=ctx.max_tokens,
            tools=ctx.tools,
            stream_to_ui=ctx.stream_to_ui,
            effort=ctx.effort,
            model_override=ctx.model_override,
            prefix_mismatch=ctx.prefix_mismatch,
        )
        try:
            call = await self.model.call(request, on_stream_event=self._stream_callback(first))
        except Exception:
            # Consumed even though the call failed, so the previous step's results (or
            # the member's message) outlive a turn that never completes.
            await self._consume(fresh, author)
            raise
        self.calls += 1
        sent = self._sent_since_history(call)
        message = call.message()
        if call.stop_reason == "refusal":
            await self._consume(sent, author)
            stop_details = as_object(message.get("stop_details")) or {}
            return self._outcome(
                "refusal", next_step=step, refusal_category=as_str(stop_details.get("category"))
            )
        complete = call.stop_reason in COMPLETE_STOP_REASONS
        content = echo_content(message) if complete else []
        # Nothing to add after this turn's own tool results, or to an opening that asks
        # for nothing: the turn may end here. Any other first call carries the turn's
        # input (the member's message, the network's news, the counterparty's message),
        # which an answer that says nothing, thinking alone included, would drop.
        may_end = call.stop_reason == "end_turn" and (step > ctx.first_step or ctx.quiet_opening)
        if not content or (not may_end and says_nothing(content)):
            await self._consume(sent, author)
            if may_end:
                return self._outcome("end_turn", next_step=step + 1)
            if not complete:
                reason = f"stopped at {call.stop_reason!r} before it was complete"
            else:
                reason = "has no text or tool call" if content else "has no content"
            raise TurnIncomplete(f"the response {reason}", stop_reason=call.stop_reason)
        await self._append([*sent, WireMessage.of("assistant", list[JsonValue](content))], author)
        self.stored_input_step = None
        return content

    async def _consume(self, sent: Sequence[WireMessage], author: UUID | None) -> None:
        """Store a call's input without a response. The notices are left out: a stored
        system message must be followed by an assistant turn."""
        await self._append([m for m in sent if m.role != "system"], author=author)

    def _stored_since_last_response(self, message: WireMessage) -> bool:
        for stored in reversed(self.history):
            if stored.role == "assistant" or stored.turn_id != self.ctx.turn_id:
                return False
            if stored.same_bytes(message):
                return True
        return False

    def _sent_since_history(self, call: CheckpointedCall) -> list[WireMessage]:
        """The messages the stored call sent after the stored history: exactly what
        must be appended, even when a reused call was built by an earlier attempt."""
        body: JsonValue = json.loads(call.request_text)
        sent = objects((as_object(body) or {}).get("messages"))
        known = len(self.history)
        if len(sent) < known or canonical_json(list[JsonValue](sent[:known])) != canonical_json(
            [message.as_json() for message in self.history]
        ):
            raise TranscriptConflict(
                f"stored call ({call.turn_id}, {call.step_idx}) doesn't extend the transcript"
            )
        return [
            WireMessage(role=_role(m.get("role")), content_text=canonical_json(m.get("content")))
            for m in sent[known:]
        ]

    def _stream_callback(self, first: bool) -> StreamEventCallback | None:
        """Streams the call's outgoing text; `first`: the call is the run's first, the
        only one whose bare text may be sent (`_reply`)."""
        sink = self.on_text
        if sink is None:
            return None
        outgoing = OutgoingText()
        held = (
            _BareReply(silent=not first or not self.ctx.bare_text_sends)
            if self.ctx.role == "representative"
            else None
        )
        separate = self.streamed

        async def on_event(event: Mapping[str, JsonValue]) -> None:
            nonlocal separate
            text = outgoing.feed(event)
            if held is not None:
                text = held.filter(event, text)
            if not text:
                return
            if separate:
                # A later step's text starts a new paragraph of the same live reply.
                text, separate = "\n\n" + text, False
            self.streamed = True
            sink(text)

        return on_event

    # --- One response -----------------------------------------------------------------

    async def _process(
        self, step: int, content: list[JsonObject], refused: Mapping[str, JsonObject]
    ) -> list[JsonValue] | TurnOutcome:
        """Run the response's tool calls. Returns the results to send next, or the
        outcome when the response ends the turn. A call in `refused` keeps the error
        result an earlier attempt stored for it (module docstring, Retries)."""
        calls = _calls_in(content)
        if not calls:
            return await self._reply(step, _text_in(content))
        results: list[JsonValue] = []
        ending: tuple[ToolCall, ToolExecution] | None = None
        for call in calls:
            if ending is not None:
                results.append(tool_result_block(call.id, _not_run(call, *ending), is_error=True))
                continue
            stored = refused.get(call.id)
            if stored is not None:
                results.append(stored)
                continue
            execution = await self.executor.execute(call, step_idx=step)
            if execution.effect is not None:
                self.effects.append(execution.effect)
            if execution.ends_turn:
                ending = (call, execution)
                continue
            result = execution.result if execution.result is not None else ""
            results.append(tool_result_block(call.id, result, is_error=execution.is_error))
        if ending is None:
            return results
        if results:
            await self._store_known(step + 1, results)
        call, execution = ending
        owed = OwedResult(tool_use_id=call.id, content=execution.owed_result)
        if isinstance(execution.effect, CardRaised):
            return self._outcome("awaiting_card", next_step=step + 1, owed=owed)
        return self._outcome(
            "performative", next_step=step + 1, owed=owed, performative=_performative(call.name)
        )

    async def _reply(self, step: int, text: str) -> TurnOutcome:
        # Only an answer to the opening is a reply: text after the turn's own tool
        # results answers them.
        if text.strip() and step == self.ctx.first_step:
            execution = await self.executor.text_reply(text, step_idx=step)
            if execution is not None:
                if execution.effect is not None:
                    self.effects.append(execution.effect)
                if execution.ends_turn:
                    return self._outcome(
                        "performative", next_step=step + 1, performative="send_message"
                    )
        return self._outcome("end_turn", next_step=step + 1, final_text=text or None)

    # --- Storing ----------------------------------------------------------------------

    async def _store_known(self, next_step: int, results: list[JsonValue]) -> None:
        """Store results the turn stops with, unless an earlier attempt stored them."""
        if self.stored_input_step == next_step:
            return
        await self._append([_user(results)], author=None)
        self.stored_input_step = next_step

    async def _append(self, messages: list[WireMessage], author: UUID | None) -> None:
        if not messages:
            return
        ctx = self.ctx
        await self.transcript.append(
            ctx.transcript,
            messages,
            turn_id=ctx.turn_id,
            author_member_id=author,
            position=self.position,
        )
        self.history += [m.model_copy(update={"turn_id": ctx.turn_id}) for m in messages]
        self.position += len(messages)

    def _outcome(
        self,
        stop: TurnStop,
        *,
        next_step: int,
        owed: OwedResult | None = None,
        performative: PerformativeName | None = None,
        refusal_category: str | None = None,
        final_text: str | None = None,
    ) -> TurnOutcome:
        return TurnOutcome(
            turn_id=self.ctx.turn_id,
            stop=stop,
            next_step=next_step,
            calls=self.calls,
            performative=performative,
            effects=list(self.effects),
            owed=owed,
            refusal_category=refusal_category,
            final_text=final_text,
        )


def _not_run(call: ToolCall, ended_by: ToolCall, ending: ToolExecution) -> str:
    """The result of `call`, made after `ended_by` ended the turn."""
    effect = ending.effect
    if isinstance(effect, CardRaised) and effect.auto and call.name == _SEND_NETWORK_REQUEST:
        return NOT_RUN_YET
    return NOT_RUN.format(name=ended_by.name)


@dataclass(slots=True)
class _BareReply:
    """A representative's text blocks reach the counterparty only as a bare reply, so
    they are held until the response ends without a tool call and dropped once one
    starts. `silent`: no bare reply will be sent (the turn already sent a message, or
    it had nothing to answer), so nothing is released. A `send_message` text is not
    held (`llm.outgoing`)."""

    silent: bool = False
    parts: list[str] = field(default_factory=list[str])
    tool_called: bool = False

    def filter(self, event: Mapping[str, JsonValue], text: str) -> str:
        """What of `text` (the outgoing text `event` added) may be shown now."""
        match event.get("type"):
            case "content_block_start":
                block = as_object(event.get("content_block")) or {}
                if block.get("type") == "tool_use":
                    self.tool_called = True
                    self.parts.clear()
            case "content_block_delta":
                delta = as_object(event.get("delta")) or {}
                if delta.get("type") == "text_delta":
                    if not self.tool_called:
                        self.parts.append(text)
                    return ""
            case "message_delta":
                delta = as_object(event.get("delta")) or {}
                if delta.get("stop_reason") == "end_turn" and not self.tool_called:
                    released = "".join(self.parts)
                    self.parts.clear()
                    return "" if self.silent else released
            case _:
                pass
        return text


def _own_tail(history: Sequence[WireMessage], turn_id: UUID) -> list[WireMessage]:
    """The turn's stored messages, which must be the transcript's tail."""
    start = len(history)
    while start > 0 and history[start - 1].turn_id == turn_id:
        start -= 1
    if any(message.turn_id == turn_id for message in history[:start]):
        raise TranscriptConflict(f"turn {turn_id}'s messages are not the transcript's tail")
    return list(history[start:])


def _performative(name: str) -> PerformativeName | None:
    match name:
        case "send_message" | "consult" | "close":
            return name
        case _:
            return None


def _role(value: JsonValue | None) -> Literal["user", "assistant", "system"]:
    match value:
        case "user" | "assistant" | "system":
            return value
        case _:
            raise TranscriptConflict(f"a stored request has a message with role {value!r}")


__all__ = [
    "COMPLETE_STOP_REASONS",
    "INTERRUPTED",
    "MAX_STEPS",
    "NOT_RUN",
    "NOT_RUN_YET",
    "BudgetKeys",
    "CardRaised",
    "CheckpointedClient",
    "CheckpointedModel",
    "ConsultOpened",
    "MessageHeld",
    "MessageSent",
    "OwedResult",
    "PerformativeName",
    "SearchRan",
    "TextSink",
    "ToolCall",
    "ToolExecution",
    "ToolExecutor",
    "TurnContext",
    "TurnEffect",
    "TurnIncomplete",
    "TurnOutcome",
    "TurnRole",
    "TurnStop",
    "echo_content",
    "opening_blocks",
    "run_turn",
    "says_nothing",
    "text_block",
    "tool_calls",
    "tool_result_block",
]
