"""Agent turns: every P1–P2 model activity, on `harmony-model` (§5, §7).

Each activity checks the world is live, heartbeats for its whole run, and makes its
model calls through `agents.runtime.CheckpointedClient` (`call_model_checkpointed`), so
a retried attempt replays stored responses instead of calling or spending again. Side
effects use deterministic IDs (`ids.side_effect_id(turn_id, step_idx, tool_use_id)`, or
ids derived from the consult) and commit through one `UnitOfWork` per step, so a retry
after a commit finds its rows. Each finished turn writes its `agent_turns` row
(`domain.agent_turns`): the internal and representative turns here, the answerer and
sharing check per draft rev, and each screener call in `AgentScreen`.

- `internal_turn` (on `harmony-chat`): one internal Claude turn in a conversation.
  Drains the queued member messages and network updates into the transcript, runs
  `agents.runtime.run_turn` with `InternalToolExecutor` (its `search_network` screens
  inline through `AgentScreen`), and stops at `end_turn`, `max_steps`, a refusal, or a
  card (`awaiting_card`: the workflow resumes the same `turn_id` at `next_step` with
  the card's outcome; `card_auto` when the chat sends it without review). A new turn
  is told when the chat's send mode changed (`send_mode.send_mode_notice`). Its text
  streams to the chat's owner in every org. An update look (`look`) runs under its own
  cassette thread (`keys.look_key`) and number, and calls no model when no counterparty
  wrote and no conversation ended since Claude last wrote or looked (`skipped`).
  Otherwise it opens with `transcript.look_note`, may end without a word, and can't
  search or send.
- `rep_turn`: one representative turn on a thread side, with `RepToolExecutor`: a tool
  loop that opens with the owed tool results, the counterparty's new messages and any
  queued notices, and runs until the model stops calling tools, consults, closes, or
  has a message held for a teammate. Only `send_message` reaches the counterparty.
- `answer_consult`: internal Claude answers a consult for one draft rev with its tool
  loop (`agents.consult.answer`); for the first draft, the sharing check runs when the
  mode toward this counterparty asks for it (`release.needs_sharing_check`), and
  `agents.consult.settle` releases the answer or escalates it to a person. A later rev
  is a steer's rewrite of the waiting draft, with the member's instruction, or a rev
  drafted from a member's answer to another item on its card (`reuse_from`, the
  `use_answer` Update). Before a requester-side first draft, the matcher
  (`agents.consult.match_question`) says whether it asks one of the request's cards
  again while that card waits for its answer; the draft is still its own. A draft from
  a member's earlier answer is checked at any rev, in any mode.
- `write_guidance`: a steer's guidance call (`agents.steer.write_guidance`), stored as
  the side's next guidance version, or as why it was rejected (docs/design/EDIT.md
  §3.3). Idempotent per steer.
- `rewrite_held`: a steer's rewrite of a held message (`agents.steer.rewrite_held`),
  stored as the item's reserved rev while it is still open (EDIT.md §3.5).

A representative turn opens with its side's newest guidance once (the workflow names
the version; `context.guidance_block` leaves out one the transcript already holds).

A refused reservation (`BudgetExhausted`) publishes `demo.notice {kind: "budget"}` and
leaves the activity as a non-retryable `ApplicationError` carrying the refusing cap
(`failures.budget_refused`), so the workflow can say which budget stopped it (3.5).

Run options: `policies.chat_activity_options()` for `internal_turn`,
`policies.model_activity_options()` for the rest.

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
from datetime import date, datetime
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from harmony.agents import consult as answerer
from harmony.agents import context, keys, release
from harmony.agents import steer as steering
from harmony.agents.consult import AnswerFailed, ConsultDraft, DraftReady, SharingCheck
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
    look_note,
)
from harmony.db import repo
from harmony.db.models import (
    Card,
    ChatMessage,
    Conversation,
    Escalation,
    EscalationDraft,
    Member,
    ModelCall,
    Org,
    Steer,
    Thread,
    ThreadMessage,
    World,
)
from harmony.db.models.choices import Side, SteerGuidanceOutcome
from harmony.db.uow import UnitOfWork, unit_of_work
from harmony.domain import agent_turns, clock, consults, conversations, escalations, steers
from harmony.domain import threads as thread_domain
from harmony.domain.conversations import NO_MEMBER_MESSAGE
from harmony.domain.settings import mode_toward, skips_approvals
from harmony.events.bus import StreamPublisher
from harmony.events.schemas import DemoNotice, EventScope
from harmony.events.stream import TYPING_HEARTBEAT_S, ChatStream, TurnStream, org_streams_tokens
from harmony.llm import ModelClient
from harmony.llm.budget import SpendGuard
from harmony.llm.budget_pg import HostedCaps, hosted_budget_keys
from harmony.llm.errors import BudgetExhausted, LedgerUnavailable, ModelCallError
from harmony.llm.requests import Tier, resolve_model
from harmony.llm.wire import JsonObject
from harmony.temporal.activities.network import domain_errors
from harmony.temporal.activities.send_mode import send_mode_notice
from harmony.temporal.failures import budget_refused
from harmony.temporal.policies import ensure_world_live
from harmony.temporal.workflows.payloads import MemberMessage, NetworkUpdate

HEARTBEAT_INTERVAL_S: Final = 5.0
# Code-written user turns for a representative turn with nothing else to open with: the
# requester's first turn (its representative sends the opening message), a turn after
# the counterparty ended its own without writing, and the last turn after a close.
OPEN_THREAD: Final = "The thread with {counterparty} is open. Send your opening message."
NOTHING_NEW: Final = "No new message from {counterparty} since your last turn."
THREAD_ENDED: Final = "The conversation with {counterparty} has ended."
# The first turn of the side that reopened the conversation (REOPEN.md §4.3).
REOPENED: Final = "The conversation with {counterparty} is open again."
# What a model call raises when it fails (`harmony.llm.errors`) and a person can stand
# in for. A refused reservation isn't one: it stops the thread with its reason (3.5),
# since every model call after it would be refused too.
_CALL_FAILURES: Final = (ModelCallError, LedgerUnavailable)


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
    # An update look: `turn` is the chat's look number, never a chat turn number.
    look: bool = False


class InternalTurnResult(_Payload):
    turn_id: UUID
    stop: TurnStop
    next_step: int
    effects: list[TurnEffect]
    # Set when `stop` is `awaiting_card`.
    card_id: UUID | None = None
    card_tool_use_id: str | None = None
    # The card is sent without confirmation: the workflow approves it itself.
    card_auto: bool = False
    # An update look with no news since Claude last wrote or looked: no model call,
    # nothing stored.
    skipped: bool = False
    # `stop_details.category` when `stop` is `refusal`.
    refusal_category: str | None = None


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
    # The side's newest guidance version: the turn opens with it unless the
    # representative already has it.
    guidance_version: int | None = None
    # The side's newest steer `seq` as the turn starts (`RepToolScope.steer_seq`).
    steer_seq: int = Field(default=0, ge=0)
    # The reopening side's first turn of a reopened run: with nothing else to open
    # with, it is told the conversation is open again, and asked to write.
    reopened: bool = False
    # The thread's highest message seq when this run began (`RepToolScope.run_start_seq`).
    run_start_seq: int = Field(default=0, ge=0)


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
    # A steer's rewrite (`draft_rev` > 1): the member's instruction, their unsaved
    # wording of the draft (which the rewrite starts from) and their name.
    instruction: str | None = None
    base_text: str | None = None
    member_name: str | None = None
    # A rev the `use_answer` Update reserved (`draft_rev` > 1): the item whose member
    # answer it is drafted from.
    reuse_from: UUID | None = None
    # A rev the `retry_answer` Update reserved (`draft_rev` > 1): the step Claude
    # declined, again. With a draft on the item, only the sharing check runs, on that
    # draft or on `draft_text`, the member's edit of it.
    retry: bool = False
    draft_text: str | None = None


# --- a steer's parts -----------------------------------------------------------------


class SteerPartInput(_Payload):
    world_id: UUID
    steer_id: UUID
    cassette_thread: str


class GuidanceWritten(_Payload):
    """`write_guidance`'s result: what the steer's guidance came to."""

    outcome: SteerGuidanceOutcome
    # The guidance version written, when `updated`.
    version: int | None = None


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
        typing_every_s: float = TYPING_HEARTBEAT_S,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._model = model
        self._guard = guard
        self._publisher = publisher
        self._model_override = model_override
        self._hosted_caps = hosted_caps
        self._heartbeat_interval_s = heartbeat_interval_s
        self._typing_every_s = typing_every_s

    @asynccontextmanager
    async def _budget_refusals(self, world_id: UUID) -> AsyncGenerator[None]:
        """A refused reservation, told to every viewer (the demo bar refetches the
        budget) and re-raised with its kind for the workflow."""
        try:
            yield
        except BudgetExhausted as refused:
            await self._publish_budget_notice(world_id)
            raise budget_refused(refused) from refused

    async def _publish_budget_notice(self, world_id: UUID) -> None:
        # Best effort: the notice only prompts a refetch, and the refusal must still
        # reach the workflow with its kind.
        try:
            async with unit_of_work(self._sessionmaker, world_id) as uow:
                uow.add_event(DemoNotice(kind="budget"), EventScope.demo())
                await uow.commit()
        except Exception:
            activity.logger.warning("couldn't publish the budget notice", exc_info=True)

    def _checkpointed(self, world_id: UUID) -> CheckpointedClient:
        async def budget_keys() -> tuple[str, ...]:
            return await hosted_budget_keys(
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
        async with (
            _heartbeating(self._heartbeat_interval_s),
            self._budget_refusals(turn.world_id),
            domain_errors(),
        ):
            async with self._sessionmaker() as session:
                look = await _look(session, turn) if turn.look else None
                if turn.look and look is None:
                    return InternalTurnResult(
                        turn_id=turn.turn_id, stop="end_turn", next_step=0, effects=[], skipped=True
                    )
                world = await repo_world(session, turn.world_id)
                org = await repo.get_required(session, Org, turn.world_id, turn.org_id)
                built = await context.build_internal_context(
                    session, world_id=turn.world_id, conversation_id=turn.conversation_id
                )
                today = await clock.today(session, turn.world_id)
                send_mode = (
                    await send_mode_notice(
                        session, world_id=turn.world_id, conversation_id=turn.conversation_id
                    )
                    if _new_turn(turn)
                    else None
                )
                card_sent = await _card_sent(session, turn)
                updates = await _unstored_updates(session, turn)
            notices = (
                internal_notices(turn, today, updates=updates, send_mode=send_mode)
                if look is None
                else None
            )
            cassette_thread = (
                turn.cassette_thread if look is None else keys.look_key(turn.cassette_thread)
            )
            model = self._checkpointed(turn.world_id)
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
                    cassette_thread=cassette_thread,
                    look=look is not None,
                    wrote_at=look.wrote_at if look is not None else None,
                    member_spoke=bool(turn.messages),
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
                cassette_thread=cassette_thread,
                transcript=ConversationTranscript(
                    world_id=turn.world_id, conversation_id=turn.conversation_id
                ),
                system=built.system,
                tools=built.tools,
                opening=[text_block(look.note)] if look is not None else internal_opening(turn),
                notices=notices,
                first_step=turn.first_step,
                # Resumed, the opening is the card's own result: once the request went
                # out (approved, or sent without review) it needs no answer, and Claude
                # says in a sentence that it's out (3.13 E1b, FEEDBACK_R2_PLAN decision 7).
                # A look speaks only when the news changes the member's picture.
                quiet_opening=look is not None or (card_sent and notices is None),
                # The chat's owner watches every reply, in any org.
                stream_to_ui=True,
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
            card_auto=card.auto if card is not None else False,
            refusal_category=outcome.refusal_category,
        )

    @activity.defn
    async def rep_turn(self, turn: RepTurnInput) -> RepTurnResult:
        started = time.monotonic()
        await ensure_world_live(self._sessionmaker, turn.world_id)
        attempt = activity.info().attempt
        async with (
            _heartbeating(self._heartbeat_interval_s),
            self._budget_refusals(turn.world_id),
            domain_errors(),
        ):
            async with self._sessionmaker() as session:
                world = await repo_world(session, turn.world_id)
                thread = await repo.get_required(session, Thread, turn.world_id, turn.thread_id)
                org = await repo.get_required(session, Org, turn.world_id, turn.org_id)
                counterparty_id = thread_domain.org_of(thread, thread_domain.other(turn.side))
                counterparty = await repo.get_required(session, Org, turn.world_id, counterparty_id)
                incoming = await _incoming(session, thread, counterparty_id, turn.deliver_after_seq)
                streams = await org_streams_tokens(session, turn.world_id, turn.org_id)
                today = await clock.today(session, turn.world_id)
                guidance = await context.guidance_block(
                    session,
                    turn.world_id,
                    turn.thread_id,
                    turn.side,
                    turn.guidance_version,
                    org_name=org.name,
                    counterparty_name=counterparty.name,
                    before_turn=turn.turn_id,
                )
            frozen = await self._rep_session(turn, _tier(org))
            async with unit_of_work(self._sessionmaker, turn.world_id) as uow:
                notices = await context.flush_notices(
                    uow, thread_id=turn.thread_id, side=turn.side, turn_id=turn.turn_id
                )
                await uow.commit()
            if turn.turn == 1:
                notices = _joined(context.world_date_notice(today), notices)
            fallback = _fallback(turn, thread)
            nothing_new = not (turn.owed or incoming) and fallback == NOTHING_NEW
            # Nor does a held message declined without a reason leave anything to
            # answer: text after it acknowledges the representative's own side, and
            # sent, it would be held as a new message for the same person to decline.
            # A reopened turn is asked to write, whatever it is owed.
            quiet = not turn.reopened and (
                nothing_new or (not incoming and _only_declined(turn.owed))
            )
            executor = RepToolExecutor(
                self._sessionmaker,
                RepToolScope(
                    world_id=turn.world_id,
                    thread_id=turn.thread_id,
                    side=turn.side,
                    org_id=turn.org_id,
                    counterparty_org_id=counterparty_id,
                    turn_id=turn.turn_id,
                    steer_seq=turn.steer_seq,
                    run_start_seq=turn.run_start_seq,
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
                    fallback=fallback,
                    guidance=guidance,
                ),
                notices=notices,
                stream_to_ui=streams,
                model_override=self._model_override,
                attempt=attempt,
                quiet_opening=quiet,
            )
            model = self._checkpointed(turn.world_id)
            async with TurnStream(
                self._publisher,
                world_id=turn.world_id,
                org_id=turn.org_id,
                thread_id=turn.thread_id,
                turn_id=turn.turn_id,
                attempt=attempt,
                enabled=streams,
                typing_every_s=self._typing_every_s,
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
        check that couldn't run both hand the consult to a person. A refused
        reservation does fail it, with its kind: the thread stops on the budget."""
        await ensure_world_live(self._sessionmaker, consult.world_id)
        async with (
            _heartbeating(self._heartbeat_interval_s),
            self._budget_refusals(consult.world_id),
            domain_errors(),
        ):
            model = self._checkpointed(consult.world_id)
            attempt = activity.info().attempt
            started = time.monotonic()
            reuse = await self._reuse(consult, model, attempt=attempt)
            async with self._sessionmaker() as session:
                row = await consults.get(
                    session, world_id=consult.world_id, consult_id=consult.consult_id
                )
                drafted: DraftReady | AnswerFailed | None = (
                    await _draft_to_check(session, consult) if consult.retry else None
                )
            try:
                drafted = drafted or await answerer.answer(
                    self._sessionmaker,
                    model,
                    world_id=consult.world_id,
                    consult_id=consult.consult_id,
                    draft_rev=consult.draft_rev,
                    instruction=consult.instruction,
                    base_text=consult.base_text,
                    member_name=consult.member_name,
                    reuse=reuse,
                    retry=consult.retry,
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
                sharing = await self._sharing_check(
                    consult, drafted, model, attempt=attempt, reused=reuse is not None
                )
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
                    reused_from=reuse.escalation_id if reuse is not None else None,
                    retried=consult.retry,
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

    async def _reuse(
        self, consult: AnswerConsultInput, model: CheckpointedClient, *, attempt: int
    ) -> answerer.Reuse | None:
        """The member's answer this draft starts from: the item `reuse_from` names, if
        any. A requester-side first draft starts from none, but the matcher first says
        which open card it asks again (stored as `same_as_id`, so a retry doesn't ask
        twice). A matcher call that fails on the last attempt is no match."""
        world_id = consult.world_id
        if consult.reuse_from is not None:
            async with self._sessionmaker() as session:
                return await answerer.reuse_of(session, world_id, consult.reuse_from)
        if consult.draft_rev != 1 or consult.side != "requester":
            return None
        async with self._sessionmaker() as session:
            row = await consults.get(session, world_id=world_id, consult_id=consult.consult_id)
        if row.same_as_id is None and row.status == "answering":
            started = time.monotonic()
            try:
                card_id = await answerer.match_question(
                    self._sessionmaker,
                    model,
                    world_id=world_id,
                    consult_id=consult.consult_id,
                    attempt=attempt,
                    model_override=self._model_override,
                    cassette_thread=consult.cassette_thread,
                    consult_turn=consult.consult_turn,
                )
            except _CALL_FAILURES as failed:
                if not _last_attempt(failed):
                    raise
                card_id = None
            async with unit_of_work(self._sessionmaker, world_id) as uow:
                if card_id is not None:
                    await consults.record_same_as(uow, consult.consult_id, card_id)
                await _record_match_turn(uow, consult, _elapsed_ms(started))
                await uow.commit()
        return None

    async def _sharing_check(
        self,
        consult: AnswerConsultInput,
        drafted: DraftReady | AnswerFailed,
        model: CheckpointedClient,
        *,
        attempt: int,
        reused: bool,
    ) -> SharingCheck | None:
        """The sharing check's verdict when the release decision needs one: a draft the
        answerer would release, under *Auto* toward this counterparty (read live), or
        drafted from a member's earlier answer (`reused`) in any mode, and every first
        draft while the world skips approvals. A Retry's rev is decided as a first
        draft's, under the mode alone. Any other redraft is a person's to release, so it
        is never checked."""
        first = consult.draft_rev == 1 or reused or consult.retry
        if not isinstance(drafted, DraftReady) or not first:
            return None
        async with self._sessionmaker() as session:
            thread = await repo.get_required(session, Thread, consult.world_id, consult.thread_id)
            mode = await mode_toward(
                session,
                world_id=consult.world_id,
                org_id=consult.org_id,
                counterparty_org_id=thread_domain.org_of(thread, thread_domain.other(consult.side)),
            )
            # An open item is already a person's, under Skip approvals too.
            skip_people = (
                consult.draft_rev == 1
                and not reused
                and await skips_approvals(session, consult.world_id)
            )
        if not release.needs_sharing_check(
            mode, drafted.draft, reused=reused, skip_people=skip_people
        ):
            return None
        return await answerer.check_sharing(
            self._sessionmaker,
            model,
            world_id=consult.world_id,
            consult_id=consult.consult_id,
            draft=drafted.draft,
            draft_rev=consult.draft_rev,
            reused=reused,
            attempt=attempt,
            model_override=self._model_override,
            cassette_thread=consult.cassette_thread,
            consult_turn=consult.consult_turn,
        )

    @activity.defn
    async def write_guidance(self, part: SteerPartInput) -> GuidanceWritten:
        """The steer's guidance (docs/design/EDIT.md §3.3): internal Claude's one call,
        checked, then stored as the side's next version, or as why it was rejected. A
        steer whose guidance outcome is already stored returns it and calls nothing; a
        retried attempt replays its checkpointed call."""
        await ensure_world_live(self._sessionmaker, part.world_id)
        async with (
            _heartbeating(self._heartbeat_interval_s),
            self._budget_refusals(part.world_id),
            domain_errors(),
        ):
            async with self._sessionmaker() as session:
                row = await repo.get_required(session, Steer, part.world_id, part.steer_id)
                if row.guidance_outcome is not None:
                    return GuidanceWritten(
                        outcome=row.guidance_outcome, version=row.guidance_version
                    )
            started = time.monotonic()
            parsed = await steering.write_guidance(
                self._sessionmaker,
                self._checkpointed(part.world_id),
                world_id=part.world_id,
                steer_id=part.steer_id,
                cassette_thread=part.cassette_thread,
                attempt=activity.info().attempt,
                model_override=self._model_override,
            )
            if parsed.outcome == "rejected":
                activity.logger.info(
                    "steer %s guidance rejected: %s: %s",
                    part.steer_id,
                    parsed.failure,
                    parsed.detail,
                )
            async with unit_of_work(self._sessionmaker, part.world_id) as uow:
                stored = await steers.set_guidance_outcome(
                    uow,
                    part.steer_id,
                    outcome=parsed.outcome,
                    text=parsed.text,
                    failure=parsed.failure,
                )
                await _record_steer_turn(
                    uow,
                    stored,
                    keys.steer_guidance_turn_id(stored.id),
                    steering.GUIDANCE_STEP,
                    latency_ms=_elapsed_ms(started),
                )
                written = GuidanceWritten(
                    outcome=stored.guidance_outcome or parsed.outcome,
                    version=stored.guidance_version,
                )
                await uow.commit()
        return written

    @activity.defn
    async def rewrite_held(self, part: SteerPartInput) -> int | None:
        """The steer's rewrite of its held message (docs/design/EDIT.md §3.5), stored as
        the reserved rev with rev 1's reason, only while the item is open. Returns the
        rev written, None when none was (the item was resolved, or the call refused or
        returned nothing usable). Idempotent: a written rev is returned as it is."""
        await ensure_world_live(self._sessionmaker, part.world_id)
        async with (
            _heartbeating(self._heartbeat_interval_s),
            self._budget_refusals(part.world_id),
            domain_errors(),
        ):
            async with self._sessionmaker() as session:
                row = await repo.get_required(session, Steer, part.world_id, part.steer_id)
                if row.escalation_id is None or row.draft_rev is None:
                    return None
                rev = row.draft_rev
                key = (part.world_id, row.escalation_id, rev)
                if await session.get(EscalationDraft, key) is not None:
                    return rev
                item = await repo.get_required(session, Escalation, part.world_id, key[1])
                if item.status != "open":
                    return None
            started = time.monotonic()
            rewritten = await steering.rewrite_held(
                self._sessionmaker,
                self._checkpointed(part.world_id),
                world_id=part.world_id,
                steer_id=part.steer_id,
                cassette_thread=part.cassette_thread,
                attempt=activity.info().attempt,
                model_override=self._model_override,
            )
            turn_id = keys.steer_rewrite_turn_id(part.steer_id)
            async with unit_of_work(self._sessionmaker, part.world_id) as uow:
                written = await _add_held_rewrite(uow, row, rewritten, turn_id=turn_id)
                await _record_steer_turn(
                    uow, row, turn_id, steering.REWRITE_STEP, latency_ms=_elapsed_ms(started)
                )
                await uow.commit()
        return written


async def _add_held_rewrite(
    uow: UnitOfWork, steer: Steer, rewritten: str | None, *, turn_id: UUID
) -> int | None:
    """`rewritten` as the steer's reserved rev of its held item, still open, with rev
    1's reason (why the representative's message needed a person)."""
    if rewritten is None or steer.escalation_id is None or steer.draft_rev is None:
        return None
    item = await escalations.locked(uow, steer.escalation_id)
    if item.status != "open":
        return None
    first = await escalations.first_draft(uow.session, item)
    reason = first.rationale if first is not None else ""
    draft = ConsultDraft(
        answer=rewritten,
        needs_human=True,
        needs_human_reason=reason or None,
        route=None,
        rationale=reason,
    )
    await escalations.add_draft(uow, item.id, draft, turn_id=turn_id, draft_rev=steer.draft_rev)
    return steer.draft_rev


async def _record_steer_turn(
    uow: UnitOfWork, steer: Steer, turn_id: UUID, step: int, *, latency_ms: int
) -> None:
    """One part's `agent_turns` row: the cost of its one call."""
    await agent_turns.record(
        uow,
        turn_id=turn_id,
        role="steer",
        org_id=steer.org_id,
        thread_id=steer.thread_id,
        side=steer.side,
        steps=range(step, step + 1),
        latency_ms=latency_ms,
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


async def _record_match_turn(uow: UnitOfWork, consult: AnswerConsultInput, latency_ms: int) -> None:
    """The matcher's `agent_turns` row: the cost of its one call, if it made one."""
    turn_id = keys.match_turn_id(consult.consult_id)
    called = await uow.session.scalar(
        select(ModelCall.turn_id).where(ModelCall.turn_id == turn_id).limit(1)
    )
    if called is None:
        return
    await agent_turns.record(
        uow,
        turn_id=turn_id,
        role="screener",
        org_id=consult.org_id,
        thread_id=consult.thread_id,
        side=consult.side,
        latency_ms=latency_ms,
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

# How every note of a request's end starts, to find the notes a chat holds.
_NETWORK_NOTE_PREFIX: Final = "Harmony: every conversation on request "


def internal_opening(turn: InternalTurnInput) -> list[JsonObject]:
    """A resumed turn opens with the card's result alone; a new one with the member's
    messages, or the fixed placeholder when only the network woke it (§2.2)."""
    if turn.card is not None:
        card = turn.card
        return [tool_result_block(card.tool_use_id, card.content, is_error=card.is_error)]
    return [text_block(m.text) for m in turn.messages] or [text_block(NO_MEMBER_MESSAGE)]


def internal_notices(
    turn: InternalTurnInput,
    today: date,
    *,
    updates: Sequence[NetworkUpdate],
    send_mode: str | None = None,
) -> str | None:
    """Code-written system notes: the world date on the chat's first call, the chat's
    new send mode (`send_mode_notice`, new turns only), and what changed on its
    requests (`updates`, those of the turn's the chat doesn't hold yet)."""
    notes: list[str] = []
    if turn.turn == 1 and _new_turn(turn):
        notes.append(context.world_date_notice(today))
    if send_mode is not None:
        notes.append(send_mode)
    notes += [network_note(update) for update in updates]
    return "\n\n".join(notes) or None


async def _unstored_updates(session: AsyncSession, turn: InternalTurnInput) -> list[NetworkUpdate]:
    """The turn's network updates whose note the chat doesn't hold yet. A failed or
    refused turn's updates go with the next turn, but one whose first call was stored
    (it failed later) had already delivered its notes: each goes out once."""
    if not turn.network_updates:
        return []
    stored = list(
        await session.scalars(
            select(ChatMessage.content_text).where(
                ChatMessage.world_id == turn.world_id,
                ChatMessage.conversation_id == turn.conversation_id,
                ChatMessage.role == "system",
                ChatMessage.content_text.contains(_NETWORK_NOTE_PREFIX, autoescape=True),
            )
        )
    )
    return [
        update
        for update in turn.network_updates
        if not any(network_note(update) in text for text in stored)
    ]


async def _draft_to_check(session: AsyncSession, consult: AnswerConsultInput) -> DraftReady | None:
    """A Retry's draft when its item has one: the current rev as it stands, or with the
    member's edit as its answer (`draft_text`), for the sharing check to run on again
    with no answerer call. None when the item has no draft: the answerer drafts."""
    escalation = await repo.get(
        session, Escalation, consult.world_id, answerer.escalation_id_for(consult.consult_id)
    )
    current = None if escalation is None else await escalations.current_draft(session, escalation)
    if current is None:
        return None
    draft = ConsultDraft(
        answer=(consult.draft_text or current.answer).strip(),
        needs_human=False,
        needs_human_reason=None,
        route=escalations.route_of(current),
        rationale=current.rationale,
        short_question=current.short_question,
    )
    return DraftReady(
        draft=draft,
        turn_id=current.turn_id or keys.consult_turn_id(consult.consult_id),
        step_idx=0,
    )


@dataclass(frozen=True, slots=True)
class _Look:
    """An update look that has news to check: its opening note, and when Claude last
    wrote in the chat (`get_request_status` shows the messages since)."""

    note: str
    wrote_at: datetime | None


async def _look(session: AsyncSession, turn: InternalTurnInput) -> _Look | None:
    """The look's note, naming the chat's requests where a counterparty wrote or a
    conversation ended since Claude last wrote or looked, and quoting the answers the
    chat's owner gave meanwhile that the request's other open conversations haven't had
    (`conversations.answers_to_pass_on`); None when there are none. The look's own rows
    don't count, so a retried look finds what it found the first time."""
    conversation = await repo.get_required(
        session, Conversation, turn.world_id, turn.conversation_id
    )
    wrote_at = await conversations.last_wrote_at(session, conversation, besides=turn.turn_id)
    looked_at = await conversations.last_looked_at(session, conversation, besides=turn.turn_id)
    since = max((at for at in (wrote_at, looked_at) if at is not None), default=None)
    news = await conversations.requests_with_news(session, conversation, since=since)
    answers = await conversations.answers_to_pass_on(session, conversation, since=since)
    if not news and not answers:
        return None
    owner = await repo.get_required(session, Member, turn.world_id, conversation.owner_member_id)
    return _Look(note=look_note(owner.name, news, answers), wrote_at=wrote_at)


async def _card_sent(session: AsyncSession, turn: InternalTurnInput) -> bool:
    """Whether the turn resumes on its card after the request went out: approved by the
    member, or sent without review."""
    if turn.card is None:
        return False
    card = await repo.get_required(session, Card, turn.world_id, turn.card.card_id)
    return card.status == "approved"


def _new_turn(turn: InternalTurnInput) -> bool:
    """A turn that isn't resuming after a card: notices that nudge Claude to act go
    only with a member's message or the network's news, never after each send."""
    return turn.first_step == 0 and turn.card is None


def network_note(update: NetworkUpdate) -> str:
    """The note a request's end leaves in its chat. Each round's text differs, so the
    match that keeps a note from going out twice (`_unstored_updates`) tells them apart
    (REOPEN.md §6.4)."""
    if update.round > 1:
        return (
            f"{_NETWORK_NOTE_PREFIX}{update.request_id} has ended again (round "
            f"{update.round}), after one was reopened. Check get_request_status, then tell "
            "the member how it went."
        )
    return (
        f"{_NETWORK_NOTE_PREFIX}{update.request_id} has ended. "
        "Check get_request_status, then tell the member how it went."
    )


def rep_opening(
    owed: Sequence[OwedResult],
    incoming: Sequence[str],
    *,
    counterparty_name: str,
    fallback: str = OPEN_THREAD,
    guidance: str | None = None,
) -> list[JsonObject]:
    """Owed tool results first (a held bare-text result after the rest), then the side's
    new `guidance` block, then the counterparty's new messages, each inside the
    code-written delimiter (§2.1). With neither results nor messages, the code-written
    `fallback`, after the guidance. The counterparty's words come last, so what the
    turn answers is them, not the guidance (docs/design/EDIT.md §3.6)."""
    texts = [
        context.wrap_counterparty(body, counterparty_name=counterparty_name) for body in incoming
    ]
    if not owed and not texts:
        texts = [fallback.format(counterparty=counterparty_name)]
    if guidance is not None:
        texts.insert(0, guidance)
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
    if turn.reopened:
        return REOPENED
    if thread.state in thread_domain.TERMINAL_STATES:
        return THREAD_ENDED
    if turn.side == "requester" and turn.turn == 1:
        return OPEN_THREAD
    return NOTHING_NEW


def _only_declined(owed: Sequence[OwedResult]) -> bool:
    """Whether a turn is owed nothing but held messages declined with no reason given."""
    return bool(owed) and all(result.content == answerer.HELD_DECLINED for result in owed)


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
    "REOPENED",
    "THREAD_ENDED",
    "AgentActivities",
    "AnswerConsultInput",
    "CardOutcome",
    "ConsultDecision",
    "ConsultEscalated",
    "ConsultReleased",
    "GuidanceWritten",
    "InternalTurnInput",
    "InternalTurnResult",
    "RepTurnInput",
    "RepTurnResult",
    "SteerPartInput",
    "internal_notices",
    "internal_opening",
    "network_note",
    "rep_opening",
]
