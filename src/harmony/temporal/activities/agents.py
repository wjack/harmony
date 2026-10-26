"""Agent turns: every P1–P2 model activity, on `harmony-model` (§5, §7).

Each activity checks the world is live, heartbeats for its whole run, and makes its
model calls through `agents.runtime.CheckpointedClient` (`call_model_checkpointed`), so
a retried attempt replays stored responses instead of calling or spending again. Side
effects use deterministic IDs (`ids.side_effect_id(turn_id, step_idx, tool_use_id)`, or
ids derived from the consult) and commit through one `UnitOfWork` per step, so a retry
after a commit finds its rows. Each finished turn writes its `agent_turns` row
(`domain.agent_turns`): the internal and representative turns here, the answerer and
sharing check per draft rev, and each screener call in `AgentScreen`.

- `internal_turn`: one internal Claude turn in a conversation. Drains the queued member
  messages and network updates into the transcript, runs `agents.runtime.run_turn`
  with `InternalToolExecutor` (its `search_network` screens inline through
  `AgentScreen`), and stops at `end_turn`, `max_steps`, a refusal, or a card
  (`awaiting_card`: the workflow resumes the same `turn_id` at `next_step` with the
  card's outcome).
- `rep_turn`: one representative turn on a thread side, with `RepToolExecutor`: a tool
  loop that opens with the owed tool results, the counterparty's new messages and any
  queued notices, and runs until the model stops calling tools, consults, closes, or
  has a message held for a teammate.
- `answer_consult`: internal Claude answers a consult for one draft rev with its tool
  loop (`agents.consult.answer`); for the first draft, the sharing check runs when the
  mode toward this counterparty asks for it (`release.needs_sharing_check`), and
  `agents.consult.settle` releases the answer or escalates it to a person.

Run options: `policies.model_activity_options()`.

Owner: R-temporal (the activity bodies call R-agents' runtime, tools, consult and
discovery).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from harmony.agents import consult as answerer
from harmony.agents import context, keys, release
from harmony.agents.consult import AnswerFailed, DraftReady, SharingCheck
from harmony.agents.discovery import AgentScreen
from harmony.agents.runtime import (
    CardRaised,
    CheckpointedClient,
    OwedResult,
    TurnContext,
    TurnEffect,
    TurnStop,
    opening_blocks,
    run_turn,
    text_block,
    tool_result_block,
)
from harmony.agents.tools.internal import InternalToolExecutor, InternalToolScope
from harmony.agents.tools.rep import RepToolExecutor, RepToolScope
from harmony.agents.transcript import (
    ConversationTranscript,
    RepSideTranscript,
    SqlTranscriptStore,
)
from harmony.db import repo
from harmony.db.models import Org, Thread, ThreadMessage, World
from harmony.db.models.choices import Side
from harmony.db.uow import UnitOfWork, unit_of_work
from harmony.domain import agent_turns, clock, consults
from harmony.domain import threads as thread_domain
from harmony.domain.conversations import NO_MEMBER_MESSAGE
from harmony.domain.settings import mode_toward
from harmony.events.bus import StreamPublisher
from harmony.events.stream import ChatStream, TurnStream, org_streams_tokens
from harmony.llm import ModelClient
from harmony.llm.budget import SpendGuard
from harmony.llm.budget_pg import HostedCaps, hosted_budget_keys
from harmony.llm.errors import BudgetExhausted, LedgerUnavailable, ModelCallError
from harmony.llm.requests import Tier, resolve_model
from harmony.llm.wire import JsonObject
from harmony.temporal.activities.network import domain_errors
from harmony.temporal.policies import ensure_world_live
from harmony.temporal.workflows.payloads import MemberMessage, NetworkUpdate

HEARTBEAT_INTERVAL_S: Final = 5.0
# Code-written user turns for a representative turn with nothing else to open with: the
# requester's first turn (its representative sends the opening message), a turn after
# the counterparty ended its own without writing, and the last turn after a close.
OPEN_THREAD: Final = "The thread with {counterparty} is open. Send your opening message."
NOTHING_NEW: Final = "No new message from {counterparty} since your last turn."
THREAD_ENDED: Final = "The conversation with {counterparty} has ended."
# What a model call raises when it fails (`harmony.llm.errors`).
_CALL_FAILURES: Final = (ModelCallError, BudgetExhausted, LedgerUnavailable)


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


@dataclass(frozen=True, slots=True)
class _Frozen:
    """A side's stored prompt bytes (`rep_sessions`)."""

    system_text: str
    tools_text: str


# --- internal_turn -------------------------------------------------------------------


class CardOutcome(_Payload):
    """The resolution of the card a paused internal turn raised, as the `tool_result`
    of its tool call (`tool_use_id`) that the resumed turn opens with."""

    card_id: UUID
    tool_use_id: str
    content: str
    is_error: bool = False


class InternalTurnInput(_Payload):
    world_id: UUID
    conversation_id: UUID
    org_id: UUID
    owner_member_id: UUID
    cassette_thread: str
    turn_id: UUID
    turn: int = Field(ge=1)
    # 0 for a new turn; the paused turn's `next_step` when resuming after a card.
    first_step: int = Field(default=0, ge=0)
    messages: list[MemberMessage] = []
    network_updates: list[NetworkUpdate] = []
    card: CardOutcome | None = None


class InternalTurnResult(_Payload):
    turn_id: UUID
    stop: TurnStop
    next_step: int
    effects: list[TurnEffect]
    # Set when `stop` is `awaiting_card`.
    card_id: UUID | None = None
    card_tool_use_id: str | None = None


# --- rep_turn ------------------------------------------------------------------------


class RepTurnInput(_Payload):
    world_id: UUID
    thread_id: UUID
    side: Side
    org_id: UUID
    cassette_thread: str
    turn_id: UUID
    # This side's turn number (cassette turn).
    turn: int = Field(ge=1)
    # The tool results this side's previous turns ended without, now known.
    owed: list[OwedResult] = []
    # Counterparty messages after this thread seq are new to this side.
    deliver_after_seq: int = Field(default=0, ge=0)


class RepTurnResult(_Payload):
    turn_id: UUID
    stop: TurnStop
    next_step: int
    effects: list[TurnEffect]
    # The result of the call that ended the turn, owed to this side's next turn: known
    # already, or pending (`content` None: a consult answer, a held message).
    owed: OwedResult | None = None
    refusal_category: str | None = None
    # The last thread seq this side has now seen (its next `deliver_after_seq`).
    delivered_through_seq: int = Field(default=0, ge=0)


# --- answer_consult ------------------------------------------------------------------


class AnswerConsultInput(_Payload):
    world_id: UUID
    consult_id: UUID
    thread_id: UUID
    side: Side
    org_id: UUID
    cassette_thread: str
    # The k-th consult of (thread, side): the answerer's cassette turn.
    consult_turn: int = Field(ge=1)
    draft_rev: int = Field(default=1, ge=1)
    # A member's redraft instruction.
    instruction: str | None = None


class ConsultReleased(_Payload):
    kind: Literal["released"] = "released"
    consult_id: UUID
    # The `consult` tool's result for the representative.
    owed: OwedResult


class ConsultEscalated(_Payload):
    kind: Literal["escalated"] = "escalated"
    consult_id: UUID
    escalation_id: UUID
    # The draft rev this answer wrote; None when the answerer produced no draft.
    draft_rev: int | None
    assignee_ids: list[UUID]


ConsultDecision = Annotated[ConsultReleased | ConsultEscalated, Field(discriminator="kind")]


class AgentActivities:
    """Model activities (queue `harmony-model`). Built once per worker process; each
    call reads its scenario from the world row."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        model: ModelClient,
        guard: SpendGuard,
        publisher: StreamPublisher,
        *,
        model_override: str | None,
        hosted_caps: HostedCaps | None = None,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._model = model
        self._guard = guard
        self._publisher = publisher
        self._model_override = model_override
        self._hosted_caps = hosted_caps
        self._heartbeat_interval_s = heartbeat_interval_s

    async def _checkpointed(self, world_id: UUID) -> CheckpointedClient:
        budget_keys = await hosted_budget_keys(
            self._sessionmaker, self._hosted_caps, world_id=world_id
        )
        return CheckpointedClient(
            self._sessionmaker,
            self._model,
            self._guard,
            heartbeat=activity.heartbeat,
            budget_keys=budget_keys,
        )

    @activity.defn
    async def internal_turn(self, turn: InternalTurnInput) -> InternalTurnResult:
        started = time.monotonic()
        await ensure_world_live(self._sessionmaker, turn.world_id)
        attempt = activity.info().attempt
        async with _heartbeating(self._heartbeat_interval_s), domain_errors():
            async with self._sessionmaker() as session:
                world = await repo_world(session, turn.world_id)
                org = await repo.get_required(session, Org, turn.world_id, turn.org_id)
                built = await context.build_internal_context(
                    session, world_id=turn.world_id, conversation_id=turn.conversation_id
                )
                streams = await org_streams_tokens(session, turn.world_id, turn.org_id)
                today = await clock.today(session, turn.world_id)
            model = await self._checkpointed(turn.world_id)
            screen = AgentScreen(
                self._sessionmaker,
                model,
                scenario=world.scenario,
                model_override=self._model_override,
                attempt=attempt,
            )
            executor = InternalToolExecutor(
                self._sessionmaker,
                InternalToolScope(
                    world_id=turn.world_id,
                    conversation_id=turn.conversation_id,
                    org_id=turn.org_id,
                    member_id=turn.owner_member_id,
                    turn_id=turn.turn_id,
                    turn=turn.turn,
                    cassette_thread=turn.cassette_thread,
                ),
                screen,
            )
            ctx = TurnContext(
                world_id=turn.world_id,
                scenario=world.scenario,
                role="internal",
                org_id=turn.org_id,
                org_slug=org.slug,
                tier=_tier(org),
                turn_id=turn.turn_id,
                turn=turn.turn,
                cassette_thread=turn.cassette_thread,
                transcript=ConversationTranscript(
                    world_id=turn.world_id, conversation_id=turn.conversation_id
                ),
                system=built.system,
                tools=built.tools,
                opening=internal_opening(turn),
                notices=internal_notices(turn, today),
                first_step=turn.first_step,
                stream_to_ui=streams,
                model_override=self._model_override,
                attempt=attempt,
            )
            async with ChatStream(
                self._publisher,
                world_id=turn.world_id,
                org_id=turn.org_id,
                member_id=turn.owner_member_id,
                conversation_id=turn.conversation_id,
                turn_id=turn.turn_id,
                attempt=attempt,
                enabled=streams,
            ) as stream:
                outcome = await run_turn(
                    ctx,
                    executor,
                    model,
                    transcript=SqlTranscriptStore(self._sessionmaker),
                    on_text=stream.push,
                )
            # A turn paused on a card is written again when it resumes and ends.
            async with unit_of_work(self._sessionmaker, turn.world_id) as uow:
                await agent_turns.record(
                    uow,
                    turn_id=turn.turn_id,
                    role="internal",
                    org_id=turn.org_id,
                    conversation_id=turn.conversation_id,
                    latency_ms=_elapsed_ms(started),
                )
                await uow.commit()
        card = next((e for e in outcome.effects if isinstance(e, CardRaised)), None)
        return InternalTurnResult(
            turn_id=outcome.turn_id,
            stop=outcome.stop,
            next_step=outcome.next_step,
            effects=outcome.effects,
            card_id=card.card_id if card is not None else None,
            card_tool_use_id=card.tool_use_id if card is not None else None,
        )

    @activity.defn
    async def rep_turn(self, turn: RepTurnInput) -> RepTurnResult:
        started = time.monotonic()
        await ensure_world_live(self._sessionmaker, turn.world_id)
        attempt = activity.info().attempt
        async with _heartbeating(self._heartbeat_interval_s), domain_errors():
            async with self._sessionmaker() as session:
                world = await repo_world(session, turn.world_id)
                thread = await repo.get_required(session, Thread, turn.world_id, turn.thread_id)
                org = await repo.get_required(session, Org, turn.world_id, turn.org_id)
                counterparty_id = thread_domain.org_of(thread, thread_domain.other(turn.side))
                counterparty = await repo.get_required(session, Org, turn.world_id, counterparty_id)
                incoming = await _incoming(session, thread, counterparty_id, turn.deliver_after_seq)
                streams = await org_streams_tokens(session, turn.world_id, turn.org_id)
                today = await clock.today(session, turn.world_id)
            frozen = await self._rep_session(turn, _tier(org))
            async with unit_of_work(self._sessionmaker, turn.world_id) as uow:
                notices = await context.flush_notices(
                    uow, thread_id=turn.thread_id, side=turn.side, turn_id=turn.turn_id
                )
                await uow.commit()
            if turn.turn == 1:
                notices = _joined(context.world_date_notice(today), notices)
            executor = RepToolExecutor(
                self._sessionmaker,
                RepToolScope(
                    world_id=turn.world_id,
                    thread_id=turn.thread_id,
                    side=turn.side,
                    org_id=turn.org_id,
                    counterparty_org_id=counterparty_id,
                    turn_id=turn.turn_id,
                ),
            )
            ctx = TurnContext(
                world_id=turn.world_id,
                scenario=world.scenario,
                role="representative",
                org_id=turn.org_id,
                org_slug=org.slug,
                tier=_tier(org),
                turn_id=turn.turn_id,
                turn=turn.turn,
                cassette_thread=turn.cassette_thread,
                transcript=RepSideTranscript(
                    world_id=turn.world_id, thread_id=turn.thread_id, side=turn.side
                ),
                system=frozen.system_text,
                tools=_tools(frozen.tools_text),
                opening=rep_opening(
                    turn.owed,
                    [m.body for m in incoming],
                    counterparty_name=counterparty.name,
                    fallback=_fallback(turn, thread),
                ),
                notices=notices,
                stream_to_ui=streams,
                model_override=self._model_override,
                attempt=attempt,
            )
            model = await self._checkpointed(turn.world_id)
            async with TurnStream(
                self._publisher,
                world_id=turn.world_id,
                org_id=turn.org_id,
                thread_id=turn.thread_id,
                turn_id=turn.turn_id,
                attempt=attempt,
                enabled=streams,
            ) as stream:
                outcome = await run_turn(
                    ctx,
                    executor,
                    model,
                    transcript=SqlTranscriptStore(self._sessionmaker),
                    on_text=stream.push,
                )
            async with unit_of_work(self._sessionmaker, turn.world_id) as uow:
                await agent_turns.record(
                    uow,
                    turn_id=turn.turn_id,
                    role="representative",
                    org_id=turn.org_id,
                    thread_id=turn.thread_id,
                    side=turn.side,
                    latency_ms=_elapsed_ms(started),
                )
                await uow.commit()
        delivered = max([turn.deliver_after_seq, *(m.seq for m in incoming)])
        return RepTurnResult(
            turn_id=outcome.turn_id,
            stop=outcome.stop,
            next_step=outcome.next_step,
            effects=outcome.effects,
            owed=outcome.owed,
            refusal_category=outcome.refusal_category,
            delivered_through_seq=delivered,
        )

    async def _rep_session(self, turn: RepTurnInput, tier: Tier) -> _Frozen:
        """The side's frozen system and tools: rendered and stored at its first turn."""
        async with self._sessionmaker() as session:
            stored = await context.load_rep_session(
                session, world_id=turn.world_id, thread_id=turn.thread_id, side=turn.side
            )
            if stored is not None:
                return _Frozen(stored.system_text, stored.tools_text)
            rendered = await context.build_rep_context(
                session,
                world_id=turn.world_id,
                thread_id=turn.thread_id,
                side=turn.side,
                model=resolve_model("representative", tier, self._model_override),
            )
        async with unit_of_work(self._sessionmaker, turn.world_id) as uow:
            frozen = await context.freeze_rep_session(uow, rendered)
            text = _Frozen(frozen.system_text, frozen.tools_text)
            await uow.commit()
        return text

    @activity.defn
    async def answer_consult(self, consult: AnswerConsultInput) -> ConsultDecision:
        """Draft the answer with internal Claude's tools, check it against what the org
        shares when the mode asks for that, then release it or escalate it (§4). A
        redraft only adds a draft rev to the open escalation: a person decides it. A
        retried attempt replays its checkpointed calls, and `settle` reports what the
        consult already settled to.

        A model call that fails on the last attempt Temporal allows doesn't fail the
        activity (which would end the conversation): an answerer with no draft and a
        check that couldn't run both hand the consult to a person."""
        await ensure_world_live(self._sessionmaker, consult.world_id)
        async with _heartbeating(self._heartbeat_interval_s), domain_errors():
            async with self._sessionmaker() as session:
                row = await consults.get(
                    session, world_id=consult.world_id, consult_id=consult.consult_id
                )
            model = await self._checkpointed(consult.world_id)
            attempt = activity.info().attempt
            started = time.monotonic()
            try:
                drafted = await answerer.answer(
                    self._sessionmaker,
                    model,
                    world_id=consult.world_id,
                    consult_id=consult.consult_id,
                    draft_rev=consult.draft_rev,
                    instruction=consult.instruction,
                    attempt=attempt,
                    model_override=self._model_override,
                    cassette_thread=consult.cassette_thread,
                    consult_turn=consult.consult_turn,
                )
            except _CALL_FAILURES as failed:
                if not _last_attempt(failed):
                    raise
                drafted = AnswerFailed(
                    reason="invalid", detail=f"the answerer's call failed: {failed}"
                )
            answered_ms = _elapsed_ms(started)
            started = time.monotonic()
            try:
                sharing = await self._sharing_check(consult, drafted, model, attempt=attempt)
            except _CALL_FAILURES as failed:
                if not _last_attempt(failed):
                    raise
                sharing = answerer.failed_check("its call failed")
            checked_ms = _elapsed_ms(started)
            async with unit_of_work(self._sessionmaker, consult.world_id) as uow:
                outcome = await answerer.settle(
                    uow,
                    consult_id=consult.consult_id,
                    outcome=drafted,
                    draft_rev=consult.draft_rev,
                    sharing=sharing,
                )
                await _record_consult_turns(
                    uow, consult, answered_ms, checked_ms if sharing is not None else None
                )
                await uow.commit()
        match outcome:
            case answerer.Answered():
                return ConsultReleased(
                    consult_id=outcome.consult_id,
                    owed=OwedResult(tool_use_id=row.tool_use_id or "", content=outcome.result_text),
                )
            case answerer.Escalated():
                return ConsultEscalated(
                    consult_id=outcome.consult_id,
                    escalation_id=outcome.escalation_id,
                    draft_rev=outcome.draft_rev,
                    assignee_ids=outcome.assignee_ids,
                )

    async def _sharing_check(
        self,
        consult: AnswerConsultInput,
        drafted: DraftReady | AnswerFailed,
        model: CheckpointedClient,
        *,
        attempt: int,
    ) -> SharingCheck | None:
        """The sharing check's verdict when the release decision needs one: a first
        draft the answerer would release, under *Auto* toward this counterparty (read
        live). A redraft is a person's to release, so it is never checked."""
        if consult.draft_rev != 1 or not isinstance(drafted, DraftReady):
            return None
        async with self._sessionmaker() as session:
            thread = await repo.get_required(session, Thread, consult.world_id, consult.thread_id)
            mode = await mode_toward(
                session,
                world_id=consult.world_id,
                org_id=consult.org_id,
                counterparty_org_id=thread_domain.org_of(thread, thread_domain.other(consult.side)),
            )
        if not release.needs_sharing_check(mode, drafted.draft):
            return None
        return await answerer.check_sharing(
            self._sessionmaker,
            model,
            world_id=consult.world_id,
            consult_id=consult.consult_id,
            draft=drafted.draft,
            draft_rev=consult.draft_rev,
            attempt=attempt,
            model_override=self._model_override,
            cassette_thread=consult.cassette_thread,
            consult_turn=consult.consult_turn,
        )


async def _record_consult_turns(
    uow: UnitOfWork, consult: AnswerConsultInput, answered_ms: int, checked_ms: int | None
) -> None:
    """The draft rev's answerer row and, when one ran, its sharing check's."""
    rev = consult.draft_rev
    answer_turn = keys.consult_turn_id(consult.consult_id)
    await agent_turns.record(
        uow,
        turn_id=agent_turns.rev_turn_id(answer_turn, rev),
        role="answerer",
        org_id=consult.org_id,
        thread_id=consult.thread_id,
        side=consult.side,
        calls_turn_id=answer_turn,
        steps=keys.answerer_steps(rev),
        latency_ms=answered_ms,
    )
    if checked_ms is None:
        return
    sharing_turn = keys.sharing_turn_id(consult.consult_id)
    await agent_turns.record(
        uow,
        turn_id=agent_turns.rev_turn_id(sharing_turn, rev),
        role="sharing_check",
        org_id=consult.org_id,
        thread_id=consult.thread_id,
        side=consult.side,
        calls_turn_id=sharing_turn,
        # One call per rev, at step rev - 1 (`keys.sharing_turn_id`).
        steps=range(rev - 1, rev),
        latency_ms=checked_ms,
    )


def _last_attempt(failed: Exception) -> bool:
    """Whether Temporal gives up on this activity after `failed`: its type isn't
    retried, or this is the last attempt the retry policy allows."""
    info = activity.info()
    policy = info.retry_policy
    if policy is None:
        return False
    if type(failed).__name__ in (policy.non_retryable_error_types or ()):
        return True
    return 0 < policy.maximum_attempts <= info.attempt


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


# --- Openings and notices ------------------------------------------------------------


def internal_opening(turn: InternalTurnInput) -> list[JsonObject]:
    """A resumed turn opens with the card's result alone; a new one with the member's
    messages, or the fixed placeholder when only the network woke it (§2.2)."""
    if turn.card is not None:
        card = turn.card
        return [tool_result_block(card.tool_use_id, card.content, is_error=card.is_error)]
    return [text_block(m.text) for m in turn.messages] or [text_block(NO_MEMBER_MESSAGE)]


def internal_notices(turn: InternalTurnInput, today: date) -> str | None:
    """Code-written system notes: the world date on the chat's first call, and what
    changed on its requests."""
    notes: list[str] = []
    if turn.turn == 1 and turn.first_step == 0 and turn.card is None:
        notes.append(context.world_date_notice(today))
    notes += [network_note(update) for update in turn.network_updates]
    return "\n\n".join(notes) or None


def network_note(update: NetworkUpdate) -> str:
    return (
        f"Harmony: every conversation on request {update.request_id} has ended. "
        "Check get_request_status, then tell the member how it went."
    )


def rep_opening(
    owed: Sequence[OwedResult],
    incoming: Sequence[str],
    *,
    counterparty_name: str,
    fallback: str = OPEN_THREAD,
) -> list[JsonObject]:
    """Owed tool results first, then the counterparty's new messages, each inside the
    code-written delimiter (§2.1). With neither, the code-written `fallback`."""
    texts = [
        context.wrap_counterparty(body, counterparty_name=counterparty_name) for body in incoming
    ]
    if not owed and not texts:
        texts = [fallback.format(counterparty=counterparty_name)]
    return opening_blocks(owed, texts)


async def repo_world(session: AsyncSession, world_id: UUID) -> World:
    world = await session.get(World, world_id)
    if world is None:
        raise LookupError(f"world {world_id} not found")
    return world


# --- Helpers -------------------------------------------------------------------------


async def _incoming(
    session: AsyncSession, thread: Thread, counterparty_id: UUID, after_seq: int
) -> list[ThreadMessage]:
    """The counterparty's messages this side hasn't seen. A `close` marker carries
    nothing: the side hears of it through the `closed` notice."""
    return list(
        await session.scalars(
            repo.in_world(ThreadMessage, thread.world_id)
            .where(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.from_org_id == counterparty_id,
                ThreadMessage.seq > after_seq,
                ThreadMessage.kind == "message",
            )
            .order_by(ThreadMessage.seq)
        )
    )


def _fallback(turn: RepTurnInput, thread: Thread) -> str:
    """What a turn opens with when nothing is owed and nothing new came in."""
    if thread.state in thread_domain.TERMINAL_STATES:
        return THREAD_ENDED
    if turn.side == "requester" and turn.turn == 1:
        return OPEN_THREAD
    return NOTHING_NEW


def _tier(org: Org) -> Tier:
    return "featured" if org.featured else "background"


def _tools(text: str) -> list[dict[str, JsonValue]]:
    tools = json.loads(text)
    return [tool for tool in tools if isinstance(tool, dict)]


def _joined(*parts: str | None) -> str | None:
    return "\n\n".join(part for part in parts if part) or None


@asynccontextmanager
async def _heartbeating(interval_s: float) -> AsyncGenerator[None]:
    """Heartbeat every `interval_s` while the block runs, whatever it is waiting on."""

    async def beat() -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(interval_s)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


__all__ = [
    "NOTHING_NEW",
    "OPEN_THREAD",
    "THREAD_ENDED",
    "AgentActivities",
    "AnswerConsultInput",
    "CardOutcome",
    "ConsultDecision",
    "ConsultEscalated",
    "ConsultReleased",
    "InternalTurnInput",
    "InternalTurnResult",
    "RepTurnInput",
    "RepTurnResult",
    "internal_notices",
    "internal_opening",
    "network_note",
    "rep_opening",
]
