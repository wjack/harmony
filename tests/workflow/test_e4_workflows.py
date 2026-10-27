"""The network workflows on the time-skipping test server, with scripted stand-ins for
their activities (§15): the thread's turn loop, close and the other side's last turn,
consult escalations and their Updates, messages held for a teammate, the turn cap and
the deadline, members' Edits (`steer`), the conversation's card pause, wake-ups and
continue-as-new, and the request's sliding window.

The stand-ins record every call and answer from a script, so each test reads as the
sequence of activity calls a workflow makes. Every finished run's history is replayed
(`Replayer`) to check the workflow code is deterministic over it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from temporalio import activity, workflow
from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowHandle,
    WorkflowUpdateFailedError,
    WorkflowUpdateStage,
)
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from harmony.agents.consult import HELD_DECLINED
from harmony.agents.models import CardResolution, EscalationAction, EscalationResolution
from harmony.agents.runtime import (
    ConsultOpened,
    MessageHeld,
    MessageSent,
    OwedResult,
    TurnEffect,
    TurnStop,
)
from harmony.agents.tools.rep import CUT_OFF_CONSULT, CUT_OFF_HELD
from harmony.db.models.choices import BudgetKind, FailureReason, Side, ThreadMessageKind
from harmony.ids import turn_id_for
from harmony.temporal.activities.agents import (
    AnswerConsultInput,
    CardOutcome,
    ConsultDecision,
    ConsultEscalated,
    ConsultReleased,
    GuidanceWritten,
    InternalTurnInput,
    InternalTurnResult,
    RepTurnInput,
    RepTurnResult,
    SteerPartInput,
)
from harmony.temporal.activities.network import (
    ApplyResolutionInput,
    BeginReopenInput,
    CardResolutionApplied,
    CardResolutionInput,
    EndRoundInput,
    FinishRunInput,
    NotifyConversationInput,
    NudgeChatInput,
    OpenThreadInput,
    ReassignInput,
    RecordReopenInput,
    RecordSteerInput,
    ResolutionApplied,
    RoundEnded,
    SettledCardInput,
    SettleSteerInput,
    StartRequestInput,
    SteerRecorded,
    ThreadStateInput,
    UnsummarizedInput,
)
from harmony.temporal.policies import (
    CHAT_TASK_QUEUE,
    MAIN_TASK_QUEUE,
    MODEL_TASK_QUEUE,
    WORLD_RETIRED,
    model_retry_policy,
)
from harmony.temporal.workflows.conversation import ConversationWorkflow
from harmony.temporal.workflows.network_request import NetworkRequestWorkflow
from harmony.temporal.workflows.payloads import (
    BUDGET_EXHAUSTED_ERROR,
    CARD_NOT_PENDING_ERROR,
    CONVERSATION_IDLE,
    ESCALATION_NOT_OPEN_ERROR,
    INVALID_RESOLUTION_ERROR,
    NOT_IN_ORG_ERROR,
    STEER_CONFLICT_ERROR,
    STEER_RUNNING_ERROR,
    THREAD_CLOSED_ERROR,
    BudgetExhaustedDetails,
    CardResolved,
    ConversationCarry,
    ConversationInput,
    ConversationPending,
    EscalationResolved,
    MemberMessage,
    NetworkNews,
    NetworkRequestInput,
    NetworkUpdate,
    Reassigned,
    Reassignment,
    ReopenInput,
    RequestProgress,
    SteerRequest,
    SteerStarted,
    ThreadCarry,
    ThreadChanged,
    ThreadInput,
    ThreadProgress,
    conversation_workflow_id,
    request_workflow_id,
    thread_workflow_id,
)
from harmony.temporal.workflows.thread import ThreadWorkflow

HENDERSON, NORTHLAKE, CALDER = uuid4(), uuid4(), uuid4()
ALEX, HELEN, RAJ = uuid4(), uuid4(), uuid4()


@dataclass
class Plan:
    """One scripted rep turn."""

    effects: list[TurnEffect] = field(default_factory=list[TurnEffect])
    owed: OwedResult | None = None
    stop: TurnStop = "end_turn"


def said(kind: ThreadMessageKind = "message") -> Plan:
    """A turn that sent a message (and stopped when the model did), or closed."""
    return Plan(
        effects=[MessageSent(message_id=uuid4(), message_kind=kind)],
        owed=OwedResult(tool_use_id="toolu_close", content="Closed.") if kind == "close" else None,
        stop="performative" if kind == "close" else "end_turn",
    )


def consulting(consult_id: UUID, *, deduplicated: bool = False, answer: str | None = None) -> Plan:
    return Plan(
        effects=[ConsultOpened(consult_id=consult_id, deduplicated=deduplicated)],
        owed=OwedResult(tool_use_id="toolu_consult", content=answer),
        stop="performative",
    )


def held(escalation_id: UUID) -> Plan:
    return Plan(
        effects=[MessageHeld(escalation_id=escalation_id)],
        owed=OwedResult(tool_use_id=f"toolu_{escalation_id.hex[:6]}", content=None),
        stop="performative",
    )


class Script:
    """Stand-ins for the activities: record each call, answer from the script."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.calls: list[tuple[str, BaseModel]] = []
        self.rep: dict[tuple[str, int], Plan] = {}
        # Raised by `rep_turn` in place of a turn: for that turn, else for every turn.
        self.rep_errors: dict[tuple[str, int], ApplicationError] = {}
        self.rep_failure: ApplicationError | None = None
        # A failure in place of a decision fails that call.
        self.consults: list[ConsultDecision | ApplicationError] = []
        # What `write_guidance` returns, one per call: `updated` with the next version
        # once these run out.
        self.guidance: list[GuidanceWritten | ApplicationError] = []
        # The rev each `rewrite_held` call writes.
        self.rewrites: list[int | ApplicationError | None] = []
        # Raised by `record_steer` in place of a record, one per call.
        self.record_errors: list[ApplicationError] = []
        # The `seq` `settle_steer` reads back for a steer, if it was stored.
        self.stored_steers: dict[UUID, int] = {}
        # A failure in place of a result fails that turn.
        self.internal: list[InternalTurnResult | ApplicationError] = []
        # When each `internal_turn` was scheduled, and what the chat said of each look.
        self.internal_at: list[datetime] = []
        self.looking: list[bool] = []
        self.applied: ResolutionApplied | None = None
        # Raised by `apply_resolution` in place of a result, one per call.
        self.apply_errors: list[ApplicationError] = []
        # What `stored_resolution` reads back after a failed apply.
        self.stored: ResolutionApplied | None = None
        self.card_applied: CardResolutionApplied | None = None
        # The request an approval of each card creates.
        self.card_requests: dict[UUID, UUID] = {}
        # Raised by `apply_card_resolution` before it stores anything, one per call.
        self.card_errors: list[ApplicationError] = []
        self.settled: CardResolutionApplied | None = None
        # Transient failures `set_thread_state` raises before it succeeds.
        self.state_failures = 0
        # Raised by `record_reopen` / `begin_reopen` in place of a result, one per call.
        self.record_reopen_errors: list[ApplicationError] = []
        self.begin_errors: list[ApplicationError] = []
        # The `start_seq` `begin_reopen` returns.
        self.start_seq = 7
        # What `end_round` answers, one per call: closed in round 1 once these run out.
        self.round_ends: list[RoundEnded] = []
        # Transient failures `start_network_request` raises before it succeeds.
        self.start_failures = 0
        # An activity named here waits for its event before it answers.
        self.gates: dict[str, asyncio.Event] = {}
        # Messages `internal_turn` signals to its own chat, by turn: before it returns,
        # or just after (racing the run's continue-as-new).
        self.signal_during: dict[int, MemberMessage] = {}
        self.signal_after: dict[int, MemberMessage] = {}
        # `open_thread` starts a real `ThreadWorkflow`, and `notify_conversation`
        # signals the chat, as the activities do.
        self.real_threads = False
        self.forward_notes = False
        self._background: set[asyncio.Task[None]] = set()

    def named(self, name: str) -> list[Any]:
        return [arg for called, arg in self.calls if called == name]

    async def recorded(self, name: str) -> list[Any]:
        return self.named(name)

    async def _gate(self, name: str) -> None:
        gate = self.gates.get(name)
        if gate is not None:
            await gate.wait()

    def open_gates(self) -> None:
        for gate in self.gates.values():
            gate.set()

    # --- harmony-model ----------------------------------------------------------------

    @activity.defn(name="rep_turn")
    async def rep_turn(self, turn: RepTurnInput) -> RepTurnResult:
        self.calls.append(("rep_turn", turn))
        await self._gate("rep_turn")
        await self._gate(f"rep_turn {turn.side} {turn.turn}")
        failure = self.rep_errors.get((turn.side, turn.turn), self.rep_failure)
        if failure is not None:
            raise failure
        plan = self.rep.get((turn.side, turn.turn))
        if plan is None:
            raise ApplicationError("no scripted turn", non_retryable=True)
        return RepTurnResult(
            turn_id=turn.turn_id,
            stop=plan.stop,
            next_step=1,
            effects=plan.effects,
            owed=plan.owed,
            delivered_through_seq=turn.deliver_after_seq + 1,
        )

    @activity.defn(name="answer_consult")
    async def answer_consult(self, consult: AnswerConsultInput) -> ConsultDecision:
        self.calls.append(("answer_consult", consult))
        await self._gate("answer_consult")
        decision = self.consults.pop(0)
        if isinstance(decision, ApplicationError):
            raise decision
        return decision

    @activity.defn(name="write_guidance")
    async def write_guidance(self, part: SteerPartInput) -> GuidanceWritten:
        self.calls.append(("write_guidance", part))
        await self._gate("write_guidance")
        if not self.guidance:
            return GuidanceWritten(outcome="updated", version=len(self.named("write_guidance")))
        written = self.guidance.pop(0)
        if isinstance(written, ApplicationError):
            raise written
        return written

    @activity.defn(name="rewrite_held")
    async def rewrite_held(self, part: SteerPartInput) -> int | None:
        self.calls.append(("rewrite_held", part))
        await self._gate("rewrite_held")
        rev = self.rewrites.pop(0)
        if isinstance(rev, ApplicationError):
            raise rev
        return rev

    @activity.defn(name="internal_turn")
    async def internal_turn(self, turn: InternalTurnInput) -> InternalTurnResult:
        self.calls.append(("internal_turn", turn))
        self.internal_at.append(activity.info().current_attempt_scheduled_time)
        chat = self.client.get_workflow_handle(
            conversation_workflow_id(turn.world_id, turn.conversation_id)
        )
        if turn.look:
            pending = await chat.query("pending", result_type=ConversationPending)
            self.looking.append(pending.looking)
        message = self.signal_during.pop(turn.turn, None)
        if message is not None:
            await chat.signal("member_message", message)
        later = self.signal_after.pop(turn.turn, None)
        if later is not None:
            task = asyncio.create_task(self._signal_soon(chat, later))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        result = self.internal.pop(0)
        if isinstance(result, ApplicationError):
            raise result
        return result.model_copy(update={"turn_id": turn.turn_id})

    @staticmethod
    async def _signal_soon(chat: WorkflowHandle[Any, Any], message: MemberMessage) -> None:
        await asyncio.sleep(0.01)
        await chat.signal("member_message", message)

    # --- harmony-main -----------------------------------------------------------------

    @activity.defn(name="set_thread_state")
    async def set_thread_state(self, state: ThreadStateInput) -> None:
        self.calls.append(("set_thread_state", state))
        if self.state_failures > 0:
            self.state_failures -= 1
            raise ApplicationError("the database is down")

    @activity.defn(name="note_unsummarized")
    async def note_unsummarized(self, failed: UnsummarizedInput) -> None:
        self.calls.append(("note_unsummarized", failed))

    @activity.defn(name="notify_conversation")
    async def notify_conversation(self, notify: NotifyConversationInput) -> None:
        self.calls.append(("notify_conversation", notify))
        if self.forward_notes:
            chat = self.client.get_workflow_handle(
                conversation_workflow_id(notify.world_id, notify.conversation_id)
            )
            await chat.signal("network_update", notify.update)

    @activity.defn(name="apply_resolution")
    async def apply_resolution(self, resolution: ApplyResolutionInput) -> ResolutionApplied:
        self.calls.append(("apply_resolution", resolution))
        if self.apply_errors:
            raise self.apply_errors.pop(0)
        assert self.applied is not None
        return self.applied

    @activity.defn(name="stored_resolution")
    async def stored_resolution(self, resolution: ApplyResolutionInput) -> ResolutionApplied | None:
        self.calls.append(("stored_resolution", resolution))
        await self._gate("stored_resolution")
        return self.stored

    @activity.defn(name="record_steer")
    async def record_steer(self, record: RecordSteerInput) -> SteerRecorded:
        self.calls.append(("record_steer", record))
        await self._gate("record_steer")
        if self.record_errors:
            raise self.record_errors.pop(0)
        recorded: list[RecordSteerInput] = self.named("record_steer")
        seq = sum(1 for r in recorded if r.steer.side == record.steer.side)
        return SteerRecorded(seq=seq, author_name="Helen Park")

    @activity.defn(name="settle_steer")
    async def settle_steer(self, settle: SettleSteerInput) -> int | None:
        self.calls.append(("settle_steer", settle))
        return self.stored_steers.get(settle.steer_id)

    @activity.defn(name="reassign_escalation")
    async def reassign_escalation(self, reassign: ReassignInput) -> Reassigned:
        self.calls.append(("reassign_escalation", reassign))
        change = reassign.reassignment
        return Reassigned(escalation_id=change.escalation_id, assignees=[change.to_member_id])

    @activity.defn(name="apply_card_resolution")
    async def apply_card_resolution(self, card: CardResolutionInput) -> CardResolutionApplied:
        self.calls.append(("apply_card_resolution", card))
        await self._gate("apply_card_resolution")
        if self.card_errors:
            raise self.card_errors.pop(0)
        resolution = card.resolution
        request_id = self.card_requests.get(resolution.card_id)
        if resolution.decision == "approve" and request_id is not None:
            return CardResolutionApplied(
                resolved=CardResolved(
                    card_id=resolution.card_id, status="approved", request_id=request_id
                ),
                tool_use_id="toolu_card",
                content=f"approved; request {request_id}",
            )
        if self.card_applied is not None:
            return self.card_applied
        return CardResolutionApplied(
            resolved=CardResolved(card_id=resolution.card_id, status="rejected"),
            tool_use_id="toolu_card",
            content=f"rejected: {resolution.feedback}",
        )

    @activity.defn(name="settled_card")
    async def settled_card(self, card: SettledCardInput) -> CardResolutionApplied | None:
        self.calls.append(("settled_card", card))
        return self.settled

    @activity.defn(name="start_network_request")
    async def start_network_request(self, start: StartRequestInput) -> None:
        self.calls.append(("start_network_request", start))
        await self._gate("start_network_request")
        if self.start_failures > 0:
            self.start_failures -= 1
            raise ApplicationError("the database is down")

    @activity.defn(name="open_thread")
    async def open_thread(self, opened: OpenThreadInput) -> None:
        self.calls.append(("open_thread", opened))
        thread_id = thread_workflow_id(opened.world_id, opened.thread_id)
        if not self.real_threads:
            await self.client.start_workflow(
                FakeThread.run, id=thread_id, task_queue=MAIN_TASK_QUEUE
            )
            return
        await self.client.start_workflow(
            ThreadWorkflow.run,
            ThreadInput(
                world_id=opened.world_id,
                thread_id=opened.thread_id,
                request_id=opened.request_id,
                requester_org_id=HENDERSON,
                responder_org_id=opened.responder_org_id,
                cassette_thread=f"henderson.{opened.responder_org_id.hex[:6]}",
            ),
            id=thread_id,
            task_queue=MAIN_TASK_QUEUE,
        )

    @activity.defn(name="record_reopen")
    async def record_reopen(self, record: RecordReopenInput) -> int:
        self.calls.append(("record_reopen", record))
        if self.record_reopen_errors:
            raise self.record_reopen_errors.pop(0)
        return record.reopen.seq

    @activity.defn(name="begin_reopen")
    async def begin_reopen(self, begin: BeginReopenInput) -> int:
        self.calls.append(("begin_reopen", begin))
        if self.begin_errors:
            raise self.begin_errors.pop(0)
        return self.start_seq

    @activity.defn(name="finish_run")
    async def finish_run(self, finish: FinishRunInput) -> None:
        self.calls.append(("finish_run", finish))

    @activity.defn(name="nudge_chat")
    async def nudge_chat(self, nudge: NudgeChatInput) -> None:
        self.calls.append(("nudge_chat", nudge))

    @activity.defn(name="end_round")
    async def end_round(self, end: EndRoundInput) -> RoundEnded:
        self.calls.append(("end_round", end))
        if self.round_ends:
            return self.round_ends.pop(0)
        return RoundEnded(closed=True, round=1, conversation_id=None)


@workflow.defn(sandboxed=False)
class FakeRequest:
    """Stands in for the request workflow a thread signals."""

    def __init__(self) -> None:
        self.changes: list[ThreadChanged] = []

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)

    @workflow.signal
    def thread_changed(self, change: ThreadChanged) -> None:
        self.changes.append(change)

    @workflow.query
    def seen(self) -> list[ThreadChanged]:
        return self.changes


@workflow.defn(sandboxed=False)
class FakeThread:
    """Stands in for a thread the request workflow opened."""

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)


@asynccontextmanager
async def workers(env: WorkflowEnvironment) -> AsyncIterator[Script]:
    script = Script(env.client)
    main = Worker(
        env.client,
        task_queue=MAIN_TASK_QUEUE,
        workflows=[
            ThreadWorkflow,
            ConversationWorkflow,
            NetworkRequestWorkflow,
            FakeRequest,
            FakeThread,
        ],
        activities=[
            script.set_thread_state,
            script.note_unsummarized,
            script.notify_conversation,
            script.apply_resolution,
            script.stored_resolution,
            script.reassign_escalation,
            script.record_steer,
            script.settle_steer,
            script.apply_card_resolution,
            script.settled_card,
            script.start_network_request,
            script.open_thread,
            script.record_reopen,
            script.begin_reopen,
            script.finish_run,
            script.nudge_chat,
            script.end_round,
        ],
    )
    model = Worker(
        env.client,
        task_queue=MODEL_TASK_QUEUE,
        activities=[
            script.rep_turn,
            script.answer_consult,
            script.write_guidance,
            script.rewrite_held,
        ],
    )
    # Only here: a conversation that scheduled its turn anywhere else would never run it.
    chat = Worker(env.client, task_queue=CHAT_TASK_QUEUE, activities=[script.internal_turn])
    async with main, model, chat:
        try:
            yield script
        finally:
            # A gated activity the test left waiting finishes before the workers stop.
            script.open_gates()


async def eventually[T](
    probe: Callable[[], Awaitable[T]], until: Callable[[T], bool], what: str
) -> T:
    value = await probe()
    for _ in range(200):
        if until(value):
            return value
        await asyncio.sleep(0.05)
        value = await probe()
    raise AssertionError(f"never saw {what}: last {value!r}")


async def update_error(call: Awaitable[Any]) -> ApplicationError:
    with pytest.raises(WorkflowUpdateFailedError) as failed:
        await call
    cause = failed.value.cause
    assert isinstance(cause, ApplicationError)
    return cause


async def stop(handle: WorkflowHandle[Any, Any]) -> None:
    """Terminate a stand-in that waits forever, unless time skipping already ran it
    out to its (ten-year) run timeout while a result was awaited."""
    if (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
        await handle.terminate()


async def replays(env: WorkflowEnvironment, *handles: WorkflowHandle[Any, Any]) -> None:
    """Replay each finished run's history against today's workflow code: a
    nondeterministic change fails here rather than on a live worker."""
    replayer = Replayer(
        workflows=[ThreadWorkflow, ConversationWorkflow, NetworkRequestWorkflow],
        data_converter=env.client.data_converter,
    )
    for handle in handles:
        await replayer.replay_workflow(await handle.fetch_history())


# --- ThreadWorkflow -------------------------------------------------------------------


@dataclass(frozen=True)
class Thread:
    world_id: UUID
    thread_id: UUID
    request_id: UUID

    def input(self, **overrides: Any) -> ThreadInput:
        return ThreadInput.model_validate(
            {
                "world_id": self.world_id,
                "thread_id": self.thread_id,
                "request_id": self.request_id,
                "requester_org_id": HENDERSON,
                "responder_org_id": NORTHLAKE,
                "cassette_thread": "henderson.northlake",
                **overrides,
            }
        )


async def start_thread(
    env: WorkflowEnvironment, **overrides: Any
) -> tuple[Thread, WorkflowHandle[Any, Any], WorkflowHandle[Any, Any]]:
    thread = Thread(uuid4(), uuid4(), uuid4())
    request = await env.client.start_workflow(
        FakeRequest.run,
        id=request_workflow_id(thread.world_id, thread.request_id),
        task_queue=MAIN_TASK_QUEUE,
    )
    handle = await env.client.start_workflow(
        ThreadWorkflow.run,
        thread.input(**overrides),
        id=thread_workflow_id(thread.world_id, thread.thread_id),
        task_queue=MAIN_TASK_QUEUE,
    )
    return thread, handle, request


async def progress(handle: WorkflowHandle[Any, Any]) -> ThreadProgress:
    return await handle.query("state", result_type=ThreadProgress)


async def request_saw(request: WorkflowHandle[Any, Any]) -> list[str]:
    seen = await request.query("seen", result_type=list[ThreadChanged])
    return [change.state for change in seen]


def states(script: Script) -> list[tuple[str, str | None, str | None]]:
    """`set_thread_state` calls as (state, side_to_move, closed_by)."""
    calls: list[ThreadStateInput] = script.named("set_thread_state")
    return [(s.state, s.side_to_move, s.closed_by) for s in calls]


def endings(script: Script) -> list[tuple[str, str | None, str | None]]:
    """The terminal `set_thread_state` calls as (state, end_reason, closed_by)."""
    calls: list[ThreadStateInput] = script.named("set_thread_state")
    return [(s.state, s.end_reason, s.closed_by) for s in calls if s.state in ("closed", "expired")]


def stop_reasons(script: Script) -> list[tuple[str, str | None]]:
    """The terminal `set_thread_state` calls as (state, stop_reason)."""
    calls: list[ThreadStateInput] = script.named("set_thread_state")
    return [(s.state, s.stop_reason) for s in calls if s.state in ("closed", "expired")]


def refused(kind: BudgetKind) -> ApplicationError:
    """What a model activity raises for a reservation `kind` refused (`budget_refused`)."""
    return ApplicationError(
        f"the {kind} cap is reached",
        BudgetExhaustedDetails(kind=kind).model_dump(),
        type=BUDGET_EXHAUSTED_ERROR,
        non_retryable=True,
    )


def sides(script: Script) -> list[tuple[str, int]]:
    turns: list[RepTurnInput] = script.named("rep_turn")
    return [(t.side, t.turn) for t in turns]


def resolution(
    escalation_id: UUID, action: EscalationAction, member_id: UUID, org_id: UUID, **values: Any
) -> EscalationResolution:
    return EscalationResolution.model_validate(
        {
            "escalation_id": escalation_id,
            "action": action,
            "member_id": member_id,
            "org_id": org_id,
            **values,
        }
    )


def steer_request(
    side: Side,
    org_id: UUID,
    member_id: UUID,
    *,
    body: str = "Shorter.",
    escalation_id: UUID | None = None,
    draft_text: str | None = None,
) -> SteerRequest:
    return SteerRequest(
        steer_id=uuid4(),
        side=side,
        org_id=org_id,
        member_id=member_id,
        client_key=uuid4(),
        body=body,
        escalation_id=escalation_id,
        draft_text=draft_text,
    )


async def steer(handle: WorkflowHandle[Any, Any], request: SteerRequest) -> SteerStarted:
    """The Update as the API sends it, deduplicated by its `update_id`."""
    return await handle.execute_update(
        "steer", request, id=request.update_id, result_type=SteerStarted
    )


async def test_turns_alternate_until_a_side_closes_and_the_other_writes_its_summary(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = Plan(
            effects=[
                MessageSent(message_id=uuid4(), message_kind="message"),
                MessageSent(message_id=uuid4(), message_kind="message"),
            ]
        )
        script.rep[("requester", 2)] = said("close")
        script.rep[("responder", 2)] = said("close")
        thread, handle, request = await start_thread(env)
        result = await handle.result()

        assert (result.state, result.turns) == ("closed", 4)
        assert sides(script) == [
            ("requester", 1),
            ("responder", 1),
            ("requester", 2),
            ("responder", 2),
        ]
        turns: list[RepTurnInput] = script.named("rep_turn")
        assert turns[0].turn_id == turn_id_for(thread.world_id, thread.thread_id, HENDERSON, 1)
        assert turns[1].turn_id == turn_id_for(thread.world_id, thread.thread_id, NORTHLAKE, 1)
        # Each side receives what came in since its own last turn.
        assert [t.deliver_after_seq for t in turns] == [0, 0, 1, 1]
        # Sending handed the move over on the row; only the close is written here, and
        # it names the closer so the other side is told.
        assert states(script) == [("closed", None, "requester")]
        assert endings(script) == [("closed", "closed", "requester")]
        assert script.named("note_unsummarized") == []
        # The request hears the end once, after the last turn.
        assert await request_saw(request) == ["closed"]
        assert script.named("end_round") == []
        # The chat hears of the responder's messages, then of the end.
        nudges: list[NudgeChatInput] = script.named("nudge_chat")
        assert [n.request_id for n in nudges] == [thread.request_id] * 2
        # Last, the run stores what a reopen carries: both `close` calls are owed theirs.
        assert script.calls[-1][0] == "finish_run"
        (finished,) = script.named("finish_run")
        closed = OwedResult(tool_use_id="toolu_close", content="Closed.")
        assert finished.run == 1
        assert finished.carry.turns == {"requester": 2, "responder": 2}
        assert finished.carry.owed == {"requester": [closed], "responder": [closed]}
        await stop(request)
        await replays(env, handle)


async def test_a_consult_after_a_send_never_holds_the_move(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    """Decision 1: Henderson's first turn sends and then consults ("report to Alex").
    `rep_turn` refuses the consult, so the turn's one effect is the send: nothing waits
    on Henderson's side and Northlake moves next, where the consult used to hold the
    thread for Henderson and end in a close before Northlake ever replied."""
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.rep[("requester", 1)] = Plan(
            effects=[MessageSent(message_id=uuid4(), message_kind="message")]
        )
        script.rep[("responder", 1)] = said()
        script.rep[("requester", 2)] = said("close")
        script.rep[("responder", 2)] = said("close")
        _, handle, request = await start_thread(env)
        result = await handle.result()

        assert (result.state, result.turns) == ("closed", 4)
        assert sides(script)[:2] == [("requester", 1), ("responder", 1)]
        assert script.named("answer_consult") == []
        assert states(script) == [("closed", None, "requester")]
        assert await request_saw(request) == ["closed"]
        await stop(request)
        await replays(env, handle)


async def test_a_turn_that_sent_nothing_still_hands_the_move_over(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = Plan()
        late = OwedResult(tool_use_id="toolu_late", content="Not run: the turn ran out of steps.")
        sent = MessageSent(message_id=uuid4(), message_kind="message")
        script.rep[("requester", 2)] = Plan(effects=[sent], stop="max_steps", owed=late)
        script.rep[("responder", 2)] = said("close")
        script.rep[("requester", 3)] = Plan()
        _, handle, request = await start_thread(env)
        result = await handle.result()

        # One quiet turn isn't two: the requester's message keeps the thread going.
        assert (result.state, result.turns) == ("closed", 5)
        assert states(script) == [("active", "requester", None), ("closed", None, "responder")]
        last: RepTurnInput = script.named("rep_turn")[-1]
        assert (last.side, last.owed) == ("requester", [late])
        await stop(request)


async def test_a_requester_turn_that_leaves_nothing_sent_expires_the_thread(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        escalation_id = uuid4()
        script.rep[("requester", 1)] = held(escalation_id)
        # Told its opener was declined, the requester sends nothing else.
        script.rep[("requester", 2)] = Plan()
        script.rep[("responder", 1)] = said()
        _, handle, request = await start_thread(env)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [escalation_id],
            "the held opener",
        )
        refused = OwedResult(
            tool_use_id="", content="Not sent: your side didn't approve it.", is_error=True
        )
        script.applied = ResolutionApplied(
            resolved=EscalationResolved(escalation_id=escalation_id, action="decline"),
            owed=refused,
            side="requester",
        )
        await handle.execute_update(
            "resolve_escalation",
            resolution(escalation_id, "decline", ALEX, HENDERSON),
            result_type=EscalationResolved,
        )
        result = await handle.result()

        # Nothing reached the responder: it isn't woken to write first.
        assert (result.state, result.turns) == ("expired", 2)
        assert sides(script) == [("requester", 1), ("requester", 2)]
        assert states(script) == [("active", "requester", None), ("expired", None, None)]
        assert endings(script) == [("expired", "unsent", None)]
        assert await request_saw(request) == ["awaiting_principal", "active", "expired"]
        await stop(request)
        await replays(env, handle)


async def test_two_silent_turns_in_a_row_expire_the_thread(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        # Each side ends a turn with nothing sent, consulted or held: with bare text
        # kept private, neither will say anything more (3.13 E3).
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = Plan()
        script.rep[("requester", 2)] = Plan(stop="max_steps")
        script.rep[("responder", 2)] = said()
        _, handle, request = await start_thread(env)
        result = await handle.result()

        assert (result.state, result.turns) == ("expired", 3)
        assert sides(script) == [("requester", 1), ("responder", 1), ("requester", 2)]
        assert endings(script) == [("expired", "quiet", None)]
        assert await request_saw(request) == ["expired"]
        await stop(request)
        await replays(env, handle)


# Why a model activity fails for good, and the reason the chat or thread shows (3.5).
FAILURES: list[tuple[str, ApplicationError, FailureReason]] = [
    ("session", refused("session"), "session_budget"),
    ("client", refused("client"), "daily_budget"),
    ("daily", refused("daily"), "daily_budget"),
    ("global", refused("global"), "demo_budget"),
    ("provider", refused("provider"), "demo_budget"),
    ("paused", refused("paused"), "paused"),
    (
        "unreadable-kind",
        ApplicationError("cap", type=BUDGET_EXHAUSTED_ERROR, non_retryable=True),
        "demo_budget",
    ),
    ("400", ApplicationError("400", type="BadRequest", non_retryable=True), "model_error"),
    (
        "incomplete",
        ApplicationError("cut off", type="TurnIncomplete", non_retryable=True),
        "model_error",
    ),
    # Retried, then given up on.
    ("5xx", ApplicationError("overloaded", type="ModelCallError"), "model_error"),
]


def attempts(failure: ApplicationError) -> int:
    return 1 if failure.non_retryable else model_retry_policy().maximum_attempts


THREAD_FAILURES = [pytest.param(error, reason, id=name) for name, error, reason in FAILURES]


@pytest.mark.parametrize(("failure", "reason"), THREAD_FAILURES)
async def test_a_failed_turn_stops_the_thread_with_its_reason(
    temporal_env_time_skipping: WorkflowEnvironment,
    failure: ApplicationError,
    reason: FailureReason,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.rep[("requester", 1)] = said()
        script.rep_errors[("responder", 1)] = failure
        _, handle, request = await start_thread(env)
        result = await handle.result()

        assert (result.state, result.turns) == ("closed", 2)
        assert stop_reasons(script) == [("closed", reason)]
        assert endings(script) == [("closed", "stopped", None)]
        assert len(script.named("rep_turn")) == 1 + attempts(failure)
        assert await request_saw(request) == ["closed"]
        await stop(request)


@pytest.mark.parametrize(("failure", "reason"), THREAD_FAILURES)
async def test_a_failed_last_turn_leaves_its_side_a_note_on_a_normal_close(
    temporal_env_time_skipping: WorkflowEnvironment,
    failure: ApplicationError,
    reason: FailureReason,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = said("close")
        script.rep_errors[("requester", 2)] = failure
        thread, handle, request = await start_thread(env)
        result = await handle.result()

        assert (result.state, result.turns) == ("closed", 3)
        # The responder's close stands: the thread isn't marked stopped.
        assert stop_reasons(script) == [("closed", None)]
        notes: list[UnsummarizedInput] = script.named("note_unsummarized")
        assert [(n.org_id, n.turn_id, n.reason) for n in notes] == [
            (HENDERSON, turn_id_for(thread.world_id, thread.thread_id, HENDERSON, 2), reason)
        ]
        assert await request_saw(request) == ["closed"]
        await stop(request)
        await replays(env, handle)


async def test_a_refused_last_turn_leaves_its_side_a_note_on_a_normal_close(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = said("close")
        # The model declines to write the requester's summary: a refusal is a result,
        # not a failed activity, and must not leave the close silent.
        script.rep[("requester", 2)] = Plan(stop="refusal")
        thread, handle, request = await start_thread(env)
        result = await handle.result()

        assert (result.state, result.turns) == ("closed", 3)
        assert stop_reasons(script) == [("closed", None)]
        notes: list[UnsummarizedInput] = script.named("note_unsummarized")
        assert [(n.org_id, n.turn_id, n.reason) for n in notes] == [
            (HENDERSON, turn_id_for(thread.world_id, thread.thread_id, HENDERSON, 2), "refused")
        ]
        assert await request_saw(request) == ["closed"]
        await stop(request)
        await replays(env, handle)


async def test_a_refused_answer_stops_the_thread_rather_than_asking_a_person(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = consulting(uuid4())
        script.consults = [refused("daily")]
        _, handle, request = await start_thread(env)
        result = await handle.result()

        assert result.state == "closed"
        assert stop_reasons(script) == [("closed", "daily_budget")]
        assert endings(script) == [("closed", "stopped", None)]
        assert script.named("notify_conversation") == []
        # Waiting on the answer, then ended: nobody was asked.
        assert await request_saw(request) == ["awaiting_principal", "closed"]
        await stop(request)
        await replays(env, handle)


async def test_an_edit_rewrites_a_held_message_in_place(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    """EDIT.md §3.5: the rewrite is rev 2 of the same item, the thread keeps waiting on
    it, and approving rev 2 sends it. The next turn opens with the new guidance."""
    env = temporal_env_time_skipping
    async with workers(env) as script:
        escalation_id = uuid4()
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = held(escalation_id)
        script.rep[("requester", 2)] = said("close")
        script.rep[("responder", 2)] = said("close")
        script.rewrites = [2]
        _, handle, request = await start_thread(env)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [escalation_id],
            "the held message",
        )
        edit = steer_request(
            "responder", NORTHLAKE, HELEN, escalation_id=escalation_id, draft_text="Nov 4?"
        )
        started = await steer(handle, edit)
        assert (started.escalation_id, started.draft_rev) == (escalation_id, 2)
        await eventually(lambda: script.recorded("settle_steer"), bool, "the steer's outcome")
        (settled,) = script.named("settle_steer")
        assert (settled.steer_id, settled.failure) == (edit.steer_id, None)
        (recorded,) = script.named("record_steer")
        assert (recorded.steer, recorded.draft_rev) == (edit, 2)
        assert [part.steer_id for part in script.named("rewrite_held")] == [edit.steer_id]
        assert script.named("answer_consult") == []
        # Nothing moved while the item waited.
        assert sides(script) == [("requester", 1), ("responder", 1)]
        assert (await progress(handle)).open_escalation_ids == [escalation_id]

        approved = OwedResult(tool_use_id="", content="Sent as your side edited it: ...")
        script.applied = ResolutionApplied(
            resolved=EscalationResolved(
                escalation_id=escalation_id, action="approve", message_id=uuid4()
            ),
            owed=approved,
            side="responder",
            sent=True,
        )
        await handle.execute_update(
            "resolve_escalation",
            resolution(escalation_id, "approve", HELEN, NORTHLAKE, draft_rev=2),
            result_type=EscalationResolved,
        )
        result = await handle.result()

        assert (result.state, result.turns) == ("closed", 4)
        (applied,) = script.named("apply_resolution")
        assert applied.resolution.draft_rev == 2
        turns: list[RepTurnInput] = script.named("rep_turn")
        assert [(t.side, t.guidance_version, t.steer_seq) for t in turns] == [
            ("requester", None, 0),
            ("responder", None, 0),
            ("requester", None, 0),
            ("responder", 1, 1),
        ]
        await stop(request)
        await replays(env, handle)


async def test_a_refusal_closes_the_thread_and_the_last_turn_can_only_summarize(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = Plan(stop="refusal")
        # The requester's last turn tries to consult: nothing comes of it.
        script.rep[("requester", 2)] = consulting(uuid4())
        _, handle, request = await start_thread(env)
        result = await handle.result()

        assert (result.state, result.turns) == ("closed", 3)
        assert states(script) == [("closed", None, "responder")]
        assert endings(script) == [("closed", "stopped", "responder")]
        assert stop_reasons(script) == [("closed", "refused")]
        assert script.named("answer_consult") == []
        assert await request_saw(request) == ["closed"]
        await stop(request)
        await replays(env, handle)


async def test_a_refusal_before_anything_crossed_never_wakes_the_responder(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        # The requester's opener is refused outright: nobody wrote to the responder, so
        # it has nothing to answer or summarize (3.13 E3).
        script.rep[("requester", 1)] = Plan(stop="refusal")
        script.rep[("responder", 1)] = said("close")
        _, handle, request = await start_thread(env)
        result = await handle.result()

        assert (result.state, result.turns) == ("closed", 1)
        assert sides(script) == [("requester", 1)]
        # Who refused is stored (only they may reopen it), with no last turn for the
        # other side.
        assert endings(script) == [("closed", "stopped", "requester")]
        calls: list[ThreadStateInput] = script.named("set_thread_state")
        assert not calls[-1].last_turn
        assert stop_reasons(script) == [("closed", "refused")]
        assert script.named("note_unsummarized") == []
        assert await request_saw(request) == ["closed"]
        await stop(request)
        await replays(env, handle)

        script.calls.clear()
        script.rep.clear()
        escalation_id = uuid4()
        # Refused after its held opener was declined: still nothing crossed.
        script.rep[("requester", 1)] = held(escalation_id)
        script.rep[("requester", 2)] = Plan(stop="refusal")
        script.rep[("responder", 1)] = said("close")
        _, handle, request = await start_thread(env)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [escalation_id],
            "the held opener",
        )
        script.applied = ResolutionApplied(
            resolved=EscalationResolved(escalation_id=escalation_id, action="decline"),
            owed=OwedResult(tool_use_id="", content=HELD_DECLINED, is_error=True),
            side="requester",
        )
        await handle.execute_update(
            "resolve_escalation",
            resolution(escalation_id, "decline", ALEX, HENDERSON),
            result_type=EscalationResolved,
        )
        result = await handle.result()

        assert (result.state, result.turns) == ("closed", 2)
        assert sides(script) == [("requester", 1), ("requester", 2)]
        assert stop_reasons(script) == [("closed", "refused")]
        await stop(request)
        await replays(env, handle)


async def test_the_turn_cap_expires_a_thread_and_a_failed_turn_closes_one(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = said()
        script.rep[("requester", 2)] = said()
        _, capped, capped_request = await start_thread(env, max_turns=3)
        result = await capped.result()
        assert (result.state, result.turns) == ("expired", 3)
        assert endings(script)[-1] == ("expired", "turn_limit", None)
        assert await request_saw(capped_request) == ["expired"]

        script.rep.clear()
        script.calls.clear()
        script.rep[("requester", 1)] = said()
        # The responder's turn has no script: the activity fails for good, and nobody
        # gets a last turn.
        _, failed, failed_request = await start_thread(env)
        result = await failed.result()
        assert (result.state, result.turns) == ("closed", 2)
        assert states(script) == [("closed", None, None)]
        assert endings(script) == [("closed", "stopped", None)]
        assert stop_reasons(script) == [("closed", "model_error")]
        for request in (capped_request, failed_request):
            await stop(request)


async def test_an_escalated_consult_waits_for_a_member(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        consult_id, escalation_id = uuid4(), uuid4()
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = consulting(consult_id)
        script.rep[("responder", 2)] = said("close")
        script.rep[("requester", 2)] = said("close")
        script.consults = [
            escalated(consult_id, escalation_id, 1, HELEN),
            escalated(consult_id, escalation_id, 2, HELEN),
        ]
        thread, handle, request = await start_thread(env)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [escalation_id],
            "the escalation",
        )
        assert (await progress(handle)).state == "awaiting_principal"

        # Only Northlake's own people decide Northlake's question.
        cause = await update_error(
            handle.execute_update(
                "resolve_escalation", resolution(escalation_id, "approve", ALEX, HENDERSON)
            )
        )
        assert cause.type == NOT_IN_ORG_ERROR
        cause = await update_error(
            handle.execute_update(
                "resolve_escalation", resolution(escalation_id, "take_over", HELEN, NORTHLAKE)
            )
        )
        assert cause.type == INVALID_RESOLUTION_ERROR

        script.gates["answer_consult"] = asyncio.Event()
        started = await steer(
            handle,
            steer_request("responder", NORTHLAKE, HELEN, escalation_id=escalation_id),
        )
        assert started.draft_rev == 2

        script.applied = ResolutionApplied(
            resolved=EscalationResolved(escalation_id=escalation_id, action="approve"),
            owed=OwedResult(tool_use_id="", content="Nov 4 works."),
            side="responder",
        )
        # Rev 2 is still being drafted: approving it waits for the draft, not fails.
        approving = await handle.start_update(
            "resolve_escalation",
            resolution(escalation_id, "approve", HELEN, NORTHLAKE, draft_rev=2),
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            result_type=EscalationResolved,
        )
        script.gates["answer_consult"].set()
        resolved = await approving.result()
        assert resolved.action == "approve"
        cause = await update_error(
            handle.execute_update(
                "resolve_escalation", resolution(escalation_id, "approve", HELEN, NORTHLAKE)
            )
        )
        assert cause.type == ESCALATION_NOT_OPEN_ERROR

        result = await handle.result()
        assert (result.state, result.turns) == ("closed", 4)
        answers: list[AnswerConsultInput] = script.named("answer_consult")
        assert [(a.draft_rev, a.instruction, a.consult_turn) for a in answers] == [
            (1, None, 1),
            (2, "Shorter.", 1),
        ]
        (applied,) = script.named("apply_resolution")
        assert (applied.thread_id, applied.resolution.member_id) == (thread.thread_id, HELEN)
        # The same side moves again, opening with the released answer.
        assert sides(script) == [
            ("requester", 1),
            ("responder", 1),
            ("responder", 2),
            ("requester", 2),
        ]
        turns: list[RepTurnInput] = script.named("rep_turn")
        assert turns[2].owed == [OwedResult(tool_use_id="toolu_consult", content="Nov 4 works.")]
        assert states(script) == [
            ("awaiting_principal", "responder", None),
            ("active", "responder", None),
            ("closed", None, "responder"),
        ]
        # A responder-side question isn't the requesting member's to answer inline.
        assert script.named("notify_conversation") == []
        assert await request_saw(request) == ["awaiting_principal", "active", "closed"]
        await stop(request)
        await replays(env, handle)


async def test_a_released_answer_lets_the_same_side_go_on_and_a_repeat_is_asked_afresh(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        first, again = uuid4(), uuid4()
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = consulting(first)
        # The same question after it settled is a new consult, answered as things are now.
        script.rep[("responder", 2)] = consulting(again)
        script.rep[("responder", 3)] = said()
        script.rep[("requester", 2)] = said("close")
        script.rep[("responder", 4)] = said("close")
        script.consults = [
            ConsultReleased(
                consult_id=first,
                owed=OwedResult(tool_use_id="", content="Nov 10 at the earliest."),
            ),
            ConsultReleased(
                consult_id=again, owed=OwedResult(tool_use_id="", content="Nov 4 opened up.")
            ),
        ]
        _, handle, request = await start_thread(env)
        result = await handle.result()

        assert (result.state, result.turns) == ("closed", 6)
        asked: list[AnswerConsultInput] = script.named("answer_consult")
        assert [(a.consult_id, a.consult_turn) for a in asked] == [(first, 1), (again, 2)]
        turns: list[RepTurnInput] = script.named("rep_turn")
        assert [(t.side, t.turn, t.owed) for t in turns[2:4]] == [
            (
                "responder",
                2,
                [OwedResult(tool_use_id="toolu_consult", content="Nov 10 at the earliest.")],
            ),
            ("responder", 3, [OwedResult(tool_use_id="toolu_consult", content="Nov 4 opened up.")]),
        ]
        assert states(script) == [
            ("awaiting_principal", "responder", None),
            ("active", "responder", None),
            ("awaiting_principal", "responder", None),
            ("active", "responder", None),
            ("closed", None, "requester"),
        ]
        await stop(request)
        await replays(env, handle)


def escalated(
    consult_id: UUID, escalation_id: UUID, rev: int | None, assignee: UUID = RAJ
) -> ConsultEscalated:
    return ConsultEscalated(
        consult_id=consult_id,
        escalation_id=escalation_id,
        draft_rev=rev,
        assignee_ids=[assignee],
    )


async def test_an_approval_applies_the_rev_it_names_whatever_was_rewritten(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        consult_id, escalation_id = uuid4(), uuid4()
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = consulting(consult_id)
        script.rep[("responder", 2)] = said("close")
        script.rep[("requester", 2)] = said("close")
        script.consults = [
            escalated(consult_id, escalation_id, 1),
            escalated(consult_id, escalation_id, 2),
        ]
        _, handle, request = await start_thread(env)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [escalation_id],
            "the escalation",
        )
        await steer(
            handle,
            steer_request(
                "responder", NORTHLAKE, RAJ, body="Use the block.", escalation_id=escalation_id
            ),
        )

        def approve(**values: Any) -> Awaitable[EscalationResolved]:
            return handle.execute_update(
                "resolve_escalation",
                resolution(escalation_id, "approve", RAJ, NORTHLAKE, **values),
                result_type=EscalationResolved,
            )

        cause = await update_error(approve(draft_rev=3))
        assert cause.type == INVALID_RESOLUTION_ERROR

        script.applied = ResolutionApplied(
            resolved=EscalationResolved(escalation_id=escalation_id, action="approve"),
            owed=OwedResult(tool_use_id="", content="We can do Nov 12."),
            side="responder",
        )
        await approve(draft_rev=1)
        assert (await handle.result()).state == "closed"
        (applied,) = script.named("apply_resolution")
        assert applied.resolution.draft_rev == 1
        await stop(request)
        await replays(env, handle)


async def test_a_steer_whose_parts_fail_settles_failed_and_the_item_stays_open(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        consult_id, escalation_id = uuid4(), uuid4()
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = consulting(consult_id)
        script.rep[("responder", 2)] = said("close")
        script.rep[("requester", 2)] = said("close")
        script.consults = [
            escalated(consult_id, escalation_id, 1),
            # The answerer returned no draft for rev 2.
            escalated(consult_id, escalation_id, None),
        ]
        script.guidance = [ApplicationError("overloaded", non_retryable=True)]
        _, handle, request = await start_thread(env)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [escalation_id],
            "the escalation",
        )
        first = steer_request("responder", NORTHLAKE, RAJ, escalation_id=escalation_id)
        assert (await steer(handle, first)).draft_rev == 2
        await eventually(lambda: script.recorded("settle_steer"), bool, "the failed steer")
        (settled,) = script.named("settle_steer")
        assert (settled.steer_id, settled.failure) == (first.steer_id, "model_error")

        # Rev 2 was never written; the item still holds rev 1, and Try again is a new
        # steer that reserves the next rev.
        cause = await update_error(
            handle.execute_update(
                "resolve_escalation",
                resolution(escalation_id, "approve", RAJ, NORTHLAKE, draft_rev=2),
            )
        )
        assert cause.type == INVALID_RESOLUTION_ERROR
        script.consults = [escalated(consult_id, escalation_id, 3)]
        again = steer_request("responder", NORTHLAKE, RAJ, escalation_id=escalation_id)
        # The first steer's slot frees once its settle returns.
        assert (await retried_update(lambda: steer(handle, again))).draft_rev == 3
        await eventually(
            lambda: script.recorded("settle_steer"), lambda s: len(s) == 2, "the second steer"
        )
        script.applied = ResolutionApplied(
            resolved=EscalationResolved(escalation_id=escalation_id, action="approve"),
            owed=OwedResult(tool_use_id="", content="We can do Nov 4."),
            side="responder",
        )
        await handle.execute_update(
            "resolve_escalation",
            resolution(escalation_id, "approve", RAJ, NORTHLAKE, draft_rev=3),
            result_type=EscalationResolved,
        )
        assert (await handle.result()).state == "closed"
        assert script.named("settle_steer")[1].failure is None
        await stop(request)
        await replays(env, handle)


async def test_an_edit_with_nothing_waiting_guides_the_sides_next_turn(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    """EDIT.md §3.6: an Edit never starts a turn, and the side's next turn waits for it
    and opens with the new guidance. One steer runs per side at a time, and a retried
    POST (the same Update id) is the same steer."""
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = said()
        script.rep[("requester", 2)] = said("close")
        script.rep[("responder", 2)] = said("close")
        script.gates["rep_turn"] = asyncio.Event()
        script.gates["write_guidance"] = asyncio.Event()
        _, handle, request = await start_thread(env)
        await eventually(lambda: script.recorded("rep_turn"), bool, "the first turn")

        edit = steer_request("requester", HENDERSON, ALEX, body="Don't name the client.")
        started = await steer(handle, edit)
        assert started == SteerStarted(steer_id=edit.steer_id, escalation_id=None, draft_rev=None)
        assert await steer(handle, edit) == started
        assert len(script.named("record_steer")) == 1
        busy = await update_error(
            steer(handle, steer_request("requester", HENDERSON, uuid4(), body="Be brief."))
        )
        assert busy.type == STEER_RUNNING_ERROR
        assert busy.details == ({"by_member_id": str(ALEX)},)
        outsider = await update_error(
            steer(handle, steer_request("requester", NORTHLAKE, HELEN, body="Be brief."))
        )
        assert outsider.type == NOT_IN_ORG_ERROR

        # The responder isn't held up; the requester's next turn waits for the steer.
        script.gates["rep_turn"].set()
        await eventually(
            lambda: script.recorded("rep_turn"), lambda turns: len(turns) == 2, "the responder"
        )
        assert sides(script) == [("requester", 1), ("responder", 1)]
        script.gates["write_guidance"].set()
        result = await handle.result()

        assert (result.state, result.turns) == ("closed", 4)
        order = [name for name, _ in script.calls if name in ("settle_steer", "rep_turn")]
        assert order == ["rep_turn", "rep_turn", "settle_steer", "rep_turn", "rep_turn"]
        turns: list[RepTurnInput] = script.named("rep_turn")
        assert [(t.side, t.guidance_version, t.steer_seq) for t in turns[2:]] == [
            ("requester", 1, 1),
            ("responder", None, 0),
        ]
        await stop(request)
        await replays(env, handle)


async def test_a_steer_on_an_item_resolved_meanwhile_is_guidance_only(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    """EDIT.md §3.1's re-check: the item is approved while `record_steer` runs."""
    env = temporal_env_time_skipping
    async with workers(env) as script:
        consult_id, escalation_id = uuid4(), uuid4()
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = consulting(consult_id)
        script.rep[("responder", 2)] = said("close")
        script.rep[("requester", 2)] = said("close")
        script.consults = [escalated(consult_id, escalation_id, 1)]
        _, handle, request = await start_thread(env)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [escalation_id],
            "the escalation",
        )
        script.gates["record_steer"] = asyncio.Event()
        edit = steer_request("responder", NORTHLAKE, RAJ, escalation_id=escalation_id)
        steering = await handle.start_update(
            "steer",
            edit,
            id=edit.update_id,
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            result_type=SteerStarted,
        )
        await eventually(lambda: script.recorded("record_steer"), bool, "the steer's record")
        script.applied = ResolutionApplied(
            resolved=EscalationResolved(escalation_id=escalation_id, action="approve"),
            owed=OwedResult(tool_use_id="", content="We can do Nov 12."),
            side="responder",
        )
        await handle.execute_update(
            "resolve_escalation",
            resolution(escalation_id, "approve", RAJ, NORTHLAKE),
            result_type=EscalationResolved,
        )
        script.gates["record_steer"].set()
        started = await steering.result()

        assert (started.escalation_id, started.draft_rev) == (None, None)
        assert (await handle.result()).state == "closed"
        assert len(script.named("answer_consult")) == 1
        assert [s.steer_id for s in script.named("write_guidance")] == [edit.steer_id]
        assert [s.steer_id for s in script.named("settle_steer")] == [edit.steer_id]
        await stop(request)
        await replays(env, handle)


async def test_a_steer_that_fails_to_record_is_settled_unless_its_id_was_taken(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = said("close")
        script.rep[("requester", 2)] = said("close")
        script.gates["rep_turn"] = asyncio.Event()
        _, handle, request = await start_thread(env)
        await eventually(lambda: script.recorded("rep_turn"), bool, "the first turn")
        script.record_errors = [
            ApplicationError("the database is down", non_retryable=True),
            ApplicationError("another body", type=STEER_CONFLICT_ERROR, non_retryable=True),
        ]
        # It may have failed after its commit: a row left `working` would never settle.
        lost = steer_request("requester", HENDERSON, ALEX)
        script.stored_steers[lost.steer_id] = 1
        await update_error(steer(handle, lost))
        await eventually(lambda: script.recorded("settle_steer"), bool, "the settle")
        # The id is another submission's, which settles itself.
        taken = steer_request("requester", HENDERSON, ALEX)
        assert (await update_error(steer(handle, taken))).type == STEER_CONFLICT_ERROR
        script.gates["rep_turn"].set()

        assert (await handle.result()).state == "closed"
        settled: list[SettleSteerInput] = script.named("settle_steer")
        assert [(s.steer_id, s.failure) for s in settled] == [(lost.steer_id, "model_error")]
        # Neither ran: no part was written, and no turn waited.
        assert script.named("write_guidance") == []
        assert sides(script) == [("requester", 1), ("responder", 1), ("requester", 2)]
        # It was stored all the same: the side's later turns count it as seen, so their
        # guard doesn't hold every send for an Edit that failed.
        turns: list[RepTurnInput] = script.named("rep_turn")
        assert [t.steer_seq for t in turns] == [0, 0, 1]
        await stop(request)
        await replays(env, handle)


async def test_an_edit_that_refuses_a_close_leaves_the_move_with_its_side(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    """EDIT.md §3.7: the responder's turn chose to close, but its member's Edit arrived
    first, so the close was refused and nothing went out. That turn isn't silent: the
    responder moves again, with the guidance, instead of the requester's turn having
    nothing new and the thread expiring quiet."""
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = Plan()
        script.rep[("responder", 2)] = said("close")
        script.rep[("requester", 2)] = Plan()
        script.gates["rep_turn responder 1"] = asyncio.Event()
        _, handle, request = await start_thread(env)
        await eventually(
            lambda: script.recorded("rep_turn"), lambda turns: len(turns) == 2, "the responder"
        )
        edit = steer_request("responder", NORTHLAKE, HELEN, body="Don't close yet.")
        await steer(handle, edit)
        script.gates["rep_turn responder 1"].set()
        result = await handle.result()

        assert sides(script) == [
            ("requester", 1),
            ("responder", 1),
            ("responder", 2),
            ("requester", 2),
        ]
        assert endings(script) == [("closed", "closed", "responder")]
        guided: RepTurnInput = script.named("rep_turn")[2]
        assert (guided.guidance_version, guided.steer_seq) == (1, 1)
        assert result.state == "closed"
        await stop(request)
        await replays(env, handle)


async def test_an_apply_that_failed_after_its_commit_still_moves_the_thread_on(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        consult_id, escalation_id = uuid4(), uuid4()
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = consulting(consult_id)
        script.rep[("responder", 2)] = said("close")
        script.rep[("requester", 2)] = said("close")
        script.consults = [escalated(consult_id, escalation_id, 1)]
        # The answer was stored, then the connection dropped before the result came back.
        script.apply_errors = [ApplicationError("connection lost", non_retryable=True)]
        script.stored = ResolutionApplied(
            resolved=EscalationResolved(escalation_id=escalation_id, action="approve"),
            owed=OwedResult(tool_use_id="", content="Nov 4 works."),
            side="responder",
        )
        _, handle, request = await start_thread(env)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [escalation_id],
            "the escalation",
        )
        await update_error(
            handle.execute_update(
                "resolve_escalation", resolution(escalation_id, "approve", RAJ, NORTHLAKE)
            )
        )

        result = await handle.result()
        assert (result.state, result.turns) == ("closed", 4)
        (read,) = script.named("stored_resolution")
        assert read.resolution.escalation_id == escalation_id
        turns: list[RepTurnInput] = script.named("rep_turn")
        assert turns[2].owed == [OwedResult(tool_use_id="toolu_consult", content="Nov 4 works.")]
        await stop(request)
        await replays(env, handle)


async def test_an_apply_that_failed_before_its_commit_can_be_resolved_again(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        consult_id, escalation_id = uuid4(), uuid4()
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = consulting(consult_id)
        script.rep[("responder", 2)] = said("close")
        script.rep[("requester", 2)] = said("close")
        script.consults = [escalated(consult_id, escalation_id, 1)]
        script.apply_errors = [ApplicationError("the database is down", non_retryable=True)]
        _, handle, request = await start_thread(env)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [escalation_id],
            "the escalation",
        )
        await update_error(
            handle.execute_update(
                "resolve_escalation", resolution(escalation_id, "approve", RAJ, NORTHLAKE)
            )
        )
        await eventually(
            lambda: script.recorded("stored_resolution"), lambda r: len(r) == 1, "the read-back"
        )
        script.applied = ResolutionApplied(
            resolved=EscalationResolved(escalation_id=escalation_id, action="approve"),
            owed=OwedResult(tool_use_id="", content="Nov 4 works."),
            side="responder",
        )
        resolved = await retried_update(
            lambda: handle.execute_update(
                "resolve_escalation",
                resolution(escalation_id, "approve", RAJ, NORTHLAKE),
                result_type=EscalationResolved,
            )
        )
        assert resolved.action == "approve"
        assert (await handle.result()).state == "closed"
        assert len(script.named("apply_resolution")) == 2
        await stop(request)
        await replays(env, handle)


async def retried_update[T](call: Callable[[], Awaitable[T]]) -> T:
    """An Update a read-back still in flight rejects as not open; it is accepted once
    the read-back has found the item still open."""
    for _ in range(200):
        try:
            return await call()
        except WorkflowUpdateFailedError:
            await asyncio.sleep(0.05)
    return await call()


async def test_an_unnamed_rev_is_pinned_to_the_newest_written(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        consult_id, escalation_id = uuid4(), uuid4()
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = consulting(consult_id)
        script.rep[("responder", 2)] = said("close")
        script.rep[("requester", 2)] = said("close")
        script.consults = [escalated(consult_id, escalation_id, 1)]
        _, handle, request = await start_thread(env)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [escalation_id],
            "the escalation",
        )
        script.applied = ResolutionApplied(
            resolved=EscalationResolved(escalation_id=escalation_id, action="approve"),
            owed=OwedResult(tool_use_id="", content="We can do Nov 12."),
            side="responder",
        )
        await handle.execute_update(
            "resolve_escalation",
            resolution(escalation_id, "approve", RAJ, NORTHLAKE),
            result_type=EscalationResolved,
        )
        await handle.result()
        (applied,) = script.named("apply_resolution")
        # The activity releases exactly the rev the validator checked.
        assert applied.resolution.draft_rev == 1
        await stop(request)


async def test_the_deadline_ends_a_thread_waiting_on_its_principal(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        consult_id, escalation_id = uuid4(), uuid4()
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = consulting(consult_id)
        script.consults = [escalated(consult_id, escalation_id, 1)]
        deadline = (await env.get_current_time()) + timedelta(hours=2)
        _, handle, request = await start_thread(env, deadline_at=deadline)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [escalation_id],
            "the escalation",
        )
        # Nobody decides: the thread writes its own end instead of timing out unseen.
        result = await handle.result()
        assert (result.state, result.turns) == ("expired", 2)
        assert [state for state, _, _ in states(script)] == ["awaiting_principal", "expired"]
        assert endings(script) == [("expired", "deadline", None)]
        assert (await progress(handle)).open_escalation_ids == []
        # The consult is owed an answer should the thread be reopened.
        (finished,) = script.named("finish_run")
        assert finished.carry.owed["responder"] == [
            OwedResult(tool_use_id="toolu_consult", content=CUT_OFF_CONSULT)
        ]
        await stop(request)
        await replays(env, handle)

        script.calls.clear()
        escalation_id = uuid4()
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = held(escalation_id)
        _, handle, request = await start_thread(
            env, deadline_at=(await env.get_current_time()) + timedelta(hours=2)
        )
        assert (await handle.result()).state == "expired"
        (finished,) = script.named("finish_run")
        assert finished.carry.owed["responder"] == [
            OwedResult(
                tool_use_id=f"toolu_{escalation_id.hex[:6]}", content=CUT_OFF_HELD, is_error=True
            )
        ]
        await stop(request)
        await replays(env, handle)

        # A deadline already past ends the thread before any turn.
        script.calls.clear()
        _, handle, request = await start_thread(
            env, deadline_at=(await env.get_current_time()) - timedelta(minutes=1)
        )
        assert (await handle.result()).turns == 0
        assert endings(script) == [("expired", "deadline", None)]
        await stop(request)


async def test_a_requester_question_shows_inline_and_reassigning_needs_it_open(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        consult_id, escalation_id = uuid4(), uuid4()
        script.rep[("requester", 1)] = consulting(consult_id)
        script.consults = [escalated(consult_id, escalation_id, None, ALEX)]
        _, handle, request = await start_thread(env)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [escalation_id],
            "the escalation",
        )
        # The chat refetches from the escalation's commit: internal Claude is told nothing.
        assert script.named("notify_conversation") == []
        moved = await handle.execute_update(
            "reassign",
            Reassignment(escalation_id=escalation_id, to_member_id=RAJ, by_member_id=ALEX),
            result_type=Reassigned,
        )
        assert moved == Reassigned(escalation_id=escalation_id, assignees=[RAJ])
        cause = await update_error(
            handle.execute_update(
                "reassign",
                Reassignment(escalation_id=uuid4(), to_member_id=ALEX, by_member_id=ALEX),
            )
        )
        assert cause.type == ESCALATION_NOT_OPEN_ERROR
        # No draft was written: approving needs the member's own words.
        cause = await update_error(
            handle.execute_update(
                "resolve_escalation", resolution(escalation_id, "approve", ALEX, HENDERSON)
            )
        )
        assert cause.type == INVALID_RESOLUTION_ERROR
        await handle.terminate()
        await stop(request)


async def test_a_held_message_waits_for_a_teammate(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        first, second = uuid4(), uuid4()
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = held(first)
        script.rep[("responder", 2)] = held(second)
        script.rep[("requester", 2)] = said("close")
        script.rep[("responder", 3)] = said("close")
        _, handle, request = await start_thread(env)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [first],
            "the held message",
        )
        # Declined: nothing reaches the requester, and the responder tries again.
        refused = OwedResult(
            tool_use_id="", content="Not sent: your side didn't approve it.", is_error=True
        )
        script.applied = ResolutionApplied(
            resolved=EscalationResolved(escalation_id=first, action="decline"),
            owed=refused,
            side="responder",
        )
        await handle.execute_update(
            "resolve_escalation",
            resolution(first, "decline", HELEN, NORTHLAKE),
            result_type=EscalationResolved,
        )
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [second],
            "the second held message",
        )
        sent_id = uuid4()
        approved = OwedResult(tool_use_id="", content="Sent, approved by your side.")
        script.applied = ResolutionApplied(
            resolved=EscalationResolved(escalation_id=second, action="approve", message_id=sent_id),
            owed=approved,
            side="responder",
            sent=True,
        )
        resolved = await handle.execute_update(
            "resolve_escalation",
            resolution(second, "approve", HELEN, NORTHLAKE),
            result_type=EscalationResolved,
        )
        assert resolved.message_id == sent_id
        result = await handle.result()

        assert (result.state, result.turns) == ("closed", 5)
        turns: list[RepTurnInput] = script.named("rep_turn")
        assert [(t.side, t.turn) for t in turns] == [
            ("requester", 1),
            ("responder", 1),
            ("responder", 2),
            ("requester", 2),
            ("responder", 3),
        ]
        first_call, second_call = (f"toolu_{first.hex[:6]}", f"toolu_{second.hex[:6]}")
        assert turns[2].owed == [refused.model_copy(update={"tool_use_id": first_call})]
        assert turns[4].owed == [approved.model_copy(update={"tool_use_id": second_call})]
        # The tool holds the row itself; the workflow hands the move on afterwards.
        assert states(script) == [
            ("active", "responder", None),
            ("active", "requester", None),
            ("closed", None, "requester"),
        ]
        assert await request_saw(request) == [
            "awaiting_principal",
            "active",
            "awaiting_principal",
            "active",
            "closed",
        ]
        await stop(request)
        await replays(env, handle)


async def cancelled_activities(handle: WorkflowHandle[Any, Any]) -> list[str]:
    """The activities the run asked to cancel, by name."""
    events = (await handle.fetch_history()).events
    scheduled = {
        e.event_id: e.activity_task_scheduled_event_attributes.activity_type.name
        for e in events
        if e.HasField("activity_task_scheduled_event_attributes")
    }
    return [
        scheduled[e.activity_task_cancel_requested_event_attributes.scheduled_event_id]
        for e in events
        if e.HasField("activity_task_cancel_requested_event_attributes")
    ]


async def test_an_item_resolved_before_the_thread_tracks_it_waits_for_it(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    """An escalation is in the inbox from the answerer's commit, before `answer_consult`
    returns: deciding it then is accepted and applied once the thread tracks it."""
    env = temporal_env_time_skipping
    async with workers(env) as script:
        consult_id, escalation_id = uuid4(), uuid4()
        script.gates["answer_consult"] = asyncio.Event()
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = consulting(consult_id)
        script.rep[("responder", 2)] = said("close")
        script.rep[("requester", 2)] = said("close")
        script.consults = [escalated(consult_id, escalation_id, 1, HELEN)]
        script.applied = ResolutionApplied(
            resolved=EscalationResolved(escalation_id=escalation_id, action="approve"),
            owed=OwedResult(tool_use_id="", content="Nov 4 works."),
            side="responder",
        )
        _, handle, request = await start_thread(env)
        await eventually(lambda: script.recorded("answer_consult"), bool, "the answerer")

        def resolve(member_id: UUID, org_id: UUID) -> Awaitable[Any]:
            return handle.start_update(
                "resolve_escalation",
                resolution(escalation_id, "approve", member_id, org_id),
                wait_for_stage=WorkflowUpdateStage.ACCEPTED,
                result_type=EscalationResolved,
            )

        outsider = await resolve(ALEX, HENDERSON)
        helen = await resolve(HELEN, NORTHLAKE)
        script.gates["answer_consult"].set()
        # Checked in full once the item is tracked.
        assert (await update_error(outsider.result())).type == NOT_IN_ORG_ERROR
        assert (await helen.result()).action == "approve"
        assert (await handle.result()).state == "closed"
        (applied,) = script.named("apply_resolution")
        assert (applied.resolution.member_id, applied.resolution.draft_rev) == (HELEN, 1)
        await stop(request)
        await replays(env, handle)


async def test_a_held_message_resolved_before_its_turn_returns_waits_for_it(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        held_id = uuid4()
        script.gates["rep_turn"] = asyncio.Event()
        script.rep[("requester", 1)] = held(held_id)
        script.rep[("responder", 1)] = said("close")
        script.rep[("requester", 2)] = said("close")
        script.applied = ResolutionApplied(
            resolved=EscalationResolved(escalation_id=held_id, action="approve"),
            owed=OwedResult(tool_use_id="", content="Sent, approved by your side."),
            side="requester",
            sent=True,
        )
        _, handle, request = await start_thread(env)
        await eventually(lambda: script.recorded("rep_turn"), bool, "the first turn")
        approving = await handle.start_update(
            "resolve_escalation",
            resolution(held_id, "approve", ALEX, HENDERSON),
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            result_type=EscalationResolved,
        )
        stray = await handle.start_update(
            "reassign",
            Reassignment(escalation_id=uuid4(), to_member_id=RAJ, by_member_id=ALEX),
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
        )
        script.gates["rep_turn"].set()
        assert (await approving.result()).action == "approve"
        # An item the turn didn't open is refused once the turn is done.
        assert (await update_error(stray.result())).type == ESCALATION_NOT_OPEN_ERROR
        result = await handle.result()

        assert (result.state, result.turns) == ("closed", 3)
        assert sides(script) == [("requester", 1), ("responder", 1), ("requester", 2)]
        await stop(request)
        await replays(env, handle)


async def test_a_state_write_that_keeps_failing_delays_the_thread_without_stranding_it(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.rep[("requester", 1)] = said("close")
        script.rep[("responder", 1)] = said("close")
        # More failures than a short database activity's five attempts.
        script.state_failures = 7
        _, handle, request = await start_thread(env)
        result = await handle.result()

        assert (result.state, result.turns) == ("closed", 2)
        assert len(script.named("set_thread_state")) == 8
        assert await request_saw(request) == ["closed"]
        await stop(request)
        await replays(env, handle)


async def test_a_rewrite_stops_when_its_item_is_resolved(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        consult_id, escalation_id = uuid4(), uuid4()
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = consulting(consult_id)
        script.rep[("responder", 2)] = said("close")
        script.rep[("requester", 2)] = said("close")
        script.consults = [escalated(consult_id, escalation_id, 1)]
        _, handle, request = await start_thread(env)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [escalation_id],
            "the escalation",
        )
        # The rewrite's model call doesn't answer.
        script.gates["answer_consult"] = asyncio.Event()
        edit = steer_request("responder", NORTHLAKE, RAJ, escalation_id=escalation_id)
        await steer(handle, edit)
        await eventually(
            lambda: script.recorded("answer_consult"), lambda calls: len(calls) == 2, "the rewrite"
        )
        script.applied = ResolutionApplied(
            resolved=EscalationResolved(escalation_id=escalation_id, action="approve"),
            owed=OwedResult(tool_use_id="", content="We can do Nov 12."),
            side="responder",
        )
        await handle.execute_update(
            "resolve_escalation",
            resolution(escalation_id, "approve", RAJ, NORTHLAKE, draft_rev=1),
            result_type=EscalationResolved,
        )

        assert (await handle.result()).state == "closed"
        assert await cancelled_activities(handle) == ["answer_consult"]
        # Stopped, not failed: `settle_steer` finds the item resolved (`settled`).
        settled: list[SettleSteerInput] = script.named("settle_steer")
        assert [(s.steer_id, s.failure) for s in settled] == [(edit.steer_id, None)]
        await stop(request)
        await replays(env, handle)


async def test_a_steer_cut_short_by_the_end_of_its_thread_settles_as_closed(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        consult_id, escalation_id = uuid4(), uuid4()
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = consulting(consult_id)
        script.consults = [escalated(consult_id, escalation_id, 1)]
        # Sooner than the stalled call's heartbeat timeout, so the deadline ends it.
        deadline = (await env.get_current_time()) + timedelta(seconds=15)
        _, handle, request = await start_thread(env, deadline_at=deadline)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [escalation_id],
            "the escalation",
        )
        script.gates["answer_consult"] = asyncio.Event()
        edit = steer_request("responder", NORTHLAKE, RAJ, escalation_id=escalation_id)
        await steer(handle, edit)
        await eventually(
            lambda: script.recorded("answer_consult"), lambda calls: len(calls) == 2, "the rewrite"
        )

        # Nobody decides and the deadline passes with the rewrite still drafting.
        assert (await handle.result()).state == "expired"
        assert await cancelled_activities(handle) == ["answer_consult"]
        settled: list[SettleSteerInput] = script.named("settle_steer")
        assert [(s.steer_id, s.failure) for s in settled] == [(edit.steer_id, "thread_closed")]
        await stop(request)
        await replays(env, handle)


async def test_the_end_of_a_thread_cancels_what_is_left_in_the_order_it_started(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        consult_id, escalation_id = uuid4(), uuid4()
        script.rep[("requester", 1)] = said()
        script.rep[("responder", 1)] = consulting(consult_id)
        script.consults = [escalated(consult_id, escalation_id, 1)]
        deadline = (await env.get_current_time()) + timedelta(seconds=15)
        _, handle, request = await start_thread(env, deadline_at=deadline)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [escalation_id],
            "the escalation",
        )
        # A steer's guidance call hangs.
        script.gates["write_guidance"] = asyncio.Event()
        edit = steer_request("responder", NORTHLAKE, RAJ)
        await steer(handle, edit)
        await eventually(lambda: script.recorded("write_guidance"), bool, "the guidance call")
        # Then an approval fails, and reading it back hangs too.
        script.gates["stored_resolution"] = asyncio.Event()
        script.apply_errors = [ApplicationError("the database is down", non_retryable=True)]
        await update_error(
            handle.execute_update(
                "resolve_escalation", resolution(escalation_id, "approve", RAJ, NORTHLAKE)
            )
        )
        await eventually(lambda: script.recorded("stored_resolution"), bool, "the read-back")

        assert (await handle.result()).state == "expired"
        # Cancelled in the order they started: a replay issues the same commands.
        assert await cancelled_activities(handle) == ["write_guidance", "stored_resolution"]
        settled: list[SettleSteerInput] = script.named("settle_steer")
        assert [(s.steer_id, s.failure) for s in settled] == [(edit.steer_id, "thread_closed")]
        await stop(request)
        await replays(env, handle)


# --- Reopened runs (docs/design/REOPEN.md §4) -----------------------------------------

CLOSED_OWED = OwedResult(tool_use_id="toolu_close", content="Closed.")
REOPEN_STEER = UUID(int=7)


def carried(**overrides: Any) -> ThreadCarry:
    """What run 1 of a thread the requester closed after two turns each carries."""
    return ThreadCarry.model_validate(
        {
            "turns": {"requester": 2, "responder": 2},
            "delivered": {"requester": 3, "responder": 2},
            "consults": {"requester": 0, "responder": 1},
            "owed": {"requester": [CLOSED_OWED], "responder": [CLOSED_OWED]},
            **overrides,
        }
    )


def reopening(side: Side = "requester", *, seq: int = 2) -> ReopenInput:
    return ReopenInput(
        steer_id=REOPEN_STEER,
        side=side,
        org_id=HENDERSON if side == "requester" else NORTHLAKE,
        member_id=ALEX if side == "requester" else RAJ,
        body="Offer them our general capabilities sheet instead.",
        seq=seq,
        from_state="closed",
    )


async def start_reopened(
    env: WorkflowEnvironment, side: Side = "requester", **overrides: Any
) -> tuple[Thread, WorkflowHandle[Any, Any], WorkflowHandle[Any, Any]]:
    deadline = (await env.get_current_time()) + timedelta(hours=20)
    values: dict[str, Any] = {
        "run": 2,
        "carry": carried(),
        "reopen": reopening(side),
        "max_turns": 4 + 16,
        "deadline_at": deadline,
        "guidance": {"responder": 1},
        "steer_seq": {"requester": 1},
        **overrides,
    }
    return await start_thread(env, **values)


def attempt(script: Script) -> list[str]:
    """The reopen's activities, in order, up to its first turn."""
    names = [name for name, _ in script.calls]
    first_turn = names.index("rep_turn") if "rep_turn" in names else len(names)
    return names[:first_turn]


async def test_a_reopen_writes_guidance_then_reopens_and_its_side_moves_first(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.guidance = [GuidanceWritten(outcome="updated", version=2)]
        script.rep[("requester", 3)] = said()
        script.rep[("responder", 3)] = said("close")
        script.rep[("requester", 4)] = said("close")
        thread, handle, request = await start_reopened(env)
        result = await handle.result()

        assert (result.state, result.turns) == ("closed", 7)
        assert attempt(script) == ["record_reopen", "write_guidance", "begin_reopen"]
        (began,) = script.named("begin_reopen")
        assert (began.run, began.steer_id, began.max_turns) == (2, REOPEN_STEER, 20)
        assert sides(script) == [("requester", 3), ("responder", 3), ("requester", 4)]
        first, answer, last = script.named("rep_turn")
        # Turn numbers continue, so no run-1 turn id is reused.
        assert first.turn_id == turn_id_for(thread.world_id, thread.thread_id, HENDERSON, 3)
        assert (first.reopened, first.guidance_version, first.run_start_seq) == (True, 2, 7)
        assert first.steer_seq >= 2
        assert (first.owed, first.deliver_after_seq) == ([CLOSED_OWED], 3)
        assert (answer.reopened, answer.guidance_version, answer.run_start_seq) == (False, 1, 7)
        assert last.reopened is False
        assert endings(script) == [("closed", "closed", "responder")]
        # A later run never signals the request: it ends the round itself, then finishes.
        assert await request_saw(request) == []
        assert [r.from_workflow for r in script.named("end_round")] == [False]
        assert script.named("notify_conversation") == []
        assert script.calls[-1][0] == "finish_run"
        (finished,) = script.named("finish_run")
        assert finished.run == 2
        assert finished.carry.turns == {"requester": 4, "responder": 3}
        await stop(request)
        await replays(env, handle)


async def test_a_reopened_run_gets_its_own_turns_and_closes_the_request_in_its_round(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        conversation_id = uuid4()
        script.round_ends = [RoundEnded(closed=True, round=2, conversation_id=conversation_id)]
        script.rep[("requester", 3)] = said()
        script.rep[("responder", 3)] = said()
        # Two new turns allowed after the four carried.
        _, handle, request = await start_reopened(env, max_turns=6)
        result = await handle.result()

        assert (result.state, result.turns) == ("expired", 6)
        assert endings(script) == [("expired", "turn_limit", None)]
        notes: list[NotifyConversationInput] = script.named("notify_conversation")
        assert [(n.conversation_id, n.update.round) for n in notes] == [(conversation_id, 2)]
        await stop(request)
        await replays(env, handle)


async def test_a_responder_reopens_moves_first_and_sending_nothing_ends_it_unsent(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.rep[("responder", 3)] = Plan()
        _, handle, request = await start_reopened(env, "responder")
        result = await handle.result()

        assert result.state == "expired"
        assert sides(script) == [("responder", 3)]
        (turn,) = script.named("rep_turn")
        assert turn.reopened
        assert endings(script) == [("expired", "unsent", None)]
        await stop(request)
        await replays(env, handle)


@pytest.mark.parametrize(
    ("written", "failure"),
    [
        (refused("daily"), "daily_budget"),
        (GuidanceWritten(outcome="unchanged"), None),
        (GuidanceWritten(outcome="rejected"), None),
    ],
    ids=["budget", "unchanged", "rejected"],
)
async def test_a_reopen_whose_guidance_didnt_change_fails_privately(
    temporal_env_time_skipping: WorkflowEnvironment,
    written: GuidanceWritten | ApplicationError,
    failure: str | None,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.guidance = [written]
        _, handle, request = await start_reopened(env)
        result = await handle.result()

        # The thread is still closed as it was, and nothing shared was written.
        assert result.state == "closed"
        assert attempt(script) == ["record_reopen", "write_guidance", "settle_steer"]
        settled: list[SettleSteerInput] = script.named("settle_steer")
        assert [(s.steer_id, s.failure) for s in settled] == [(REOPEN_STEER, failure)]
        for name in ("begin_reopen", "rep_turn", "set_thread_state", "end_round", "finish_run"):
            assert script.named(name) == [], name
        await stop(request)
        await replays(env, handle)


async def test_a_reopen_that_cant_begin_settles_its_steer_failed(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.begin_errors = [
            ApplicationError("not ready", type="ReopenConflict", non_retryable=True)
        ]
        _, handle, request = await start_reopened(env)

        assert (await handle.result()).state == "closed"
        settled: list[SettleSteerInput] = script.named("settle_steer")
        assert [(s.steer_id, s.failure) for s in settled] == [(REOPEN_STEER, "reopen_failed")]
        assert script.named("rep_turn") == script.named("finish_run") == []
        await stop(request)
        await replays(env, handle)


async def test_an_edit_is_refused_until_the_reopen_begins(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.gates["write_guidance"] = asyncio.Event()
        script.rep[("requester", 3)] = said("close")
        script.rep[("responder", 3)] = said("close")
        _, handle, request = await start_reopened(env)
        await eventually(
            lambda: script.recorded("write_guidance"), bool, "the reopen's guidance call"
        )

        error = await update_error(steer(handle, steer_request("responder", NORTHLAKE, RAJ)))
        assert error.type == THREAD_CLOSED_ERROR
        script.open_gates()
        assert (await handle.result()).state == "closed"
        await stop(request)
        await replays(env, handle)


# --- ConversationWorkflow -------------------------------------------------------------


def _turn(stop: TurnStop, **values: Any) -> InternalTurnResult:
    return InternalTurnResult.model_validate(
        {"turn_id": uuid4(), "stop": stop, "next_step": 2, "effects": [], **values}
    )


def _message(text: str, card_id: UUID | None = None) -> MemberMessage:
    return MemberMessage(
        message_id=uuid4(), member_id=ALEX, text=text, sent_at=datetime.now(UTC), card_id=card_id
    )


async def start_chat(
    env: WorkflowEnvironment,
    text: str = "Find XRD time.",
    *,
    turns_per_run: int = 50,
    carried: ConversationCarry | None = None,
) -> tuple[WorkflowHandle[Any, Any], MemberMessage]:
    world_id, conversation_id = uuid4(), uuid4()
    message = _message(text)
    handle = await env.client.start_workflow(
        ConversationWorkflow.run,
        ConversationInput(
            world_id=world_id,
            conversation_id=conversation_id,
            org_id=HENDERSON,
            owner_member_id=ALEX,
            cassette_thread="chat:alex:1",
            turns_per_run=turns_per_run,
            carried=carried or ConversationCarry(),
        ),
        id=conversation_workflow_id(world_id, conversation_id),
        task_queue=MAIN_TASK_QUEUE,
        start_signal="member_message",
        start_signal_args=[message],
    )
    return handle, message


async def pending(handle: WorkflowHandle[Any, Any]) -> ConversationPending:
    return await handle.query("pending", result_type=ConversationPending)


async def test_a_card_pauses_the_turn_until_it_is_approved(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        card_id, request_id = uuid4(), uuid4()
        script.internal = [
            _turn("awaiting_card", card_id=card_id, card_tool_use_id="toolu_card"),
            _turn("end_turn"),
        ]
        script.card_applied = CardResolutionApplied(
            resolved=CardResolved(card_id=card_id, status="approved", request_id=request_id),
            tool_use_id="toolu_card",
            content=f"approved; request {request_id} sent to Northlake",
        )
        script.gates["start_network_request"] = asyncio.Event()
        handle, message = await start_chat(env)
        await eventually(
            lambda: pending(handle), lambda p: p.pending_card_id == card_id, "the card"
        )

        cause = await update_error(
            handle.execute_update(
                "resolve_card",
                CardResolution(card_id=uuid4(), decision="approve", member_id=ALEX),
            )
        )
        assert cause.type == CARD_NOT_PENDING_ERROR
        cause = await update_error(
            handle.execute_update(
                "resolve_card",
                CardResolution(card_id=card_id, decision="approve", member_id=ALEX, feedback="x"),
            )
        )
        assert cause.type == INVALID_RESOLUTION_ERROR
        resolved = await handle.execute_update(
            "resolve_card",
            CardResolution(card_id=card_id, decision="approve", member_id=ALEX),
            result_type=CardResolved,
        )
        assert (resolved.status, resolved.request_id) == ("approved", request_id)
        # Approved, the card is gone and the turn about to resume: the chat reads busy.
        assert ((p := await pending(handle)).pending_card_id, p.turn_in_flight) == (None, True)
        script.gates["start_network_request"].set()

        result = await handle.result()
        assert result.turns == 1
        first, resumed = script.named("internal_turn")
        assert first.messages == [message]
        assert (resumed.turn_id, resumed.turn, resumed.first_step) == (first.turn_id, 1, 2)
        assert resumed.card == CardOutcome(
            card_id=card_id,
            tool_use_id="toolu_card",
            content=f"approved; request {request_id} sent to Northlake",
        )
        started: list[StartRequestInput] = script.named("start_network_request")
        assert [s.request_id for s in started] == [request_id]


async def test_a_message_while_a_card_is_pending_rejects_it(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        card_id = uuid4()
        script.internal = [
            _turn("awaiting_card", card_id=card_id, card_tool_use_id="toolu_card"),
            _turn("end_turn"),
        ]
        handle, _ = await start_chat(env)
        await eventually(
            lambda: pending(handle), lambda p: p.pending_card_id == card_id, "the card"
        )
        feedback = _message("Leave Calder out.", card_id)
        await handle.signal("member_message", feedback)
        result = await handle.result()

        # The message became the card's feedback, not a new turn.
        assert result.turns == 1
        (rejected,) = script.named("apply_card_resolution")
        assert (rejected.resolution.decision, rejected.resolution.feedback) == (
            "reject",
            "Leave Calder out.",
        )
        assert script.named("start_network_request") == []
        _, resumed = script.named("internal_turn")
        assert resumed.card is not None
        assert resumed.card.content == "rejected: Leave Calder out."


async def test_a_closed_request_wakes_internal_claude(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        request_id = uuid4()
        script.internal = [_turn("end_turn"), _turn("end_turn"), _turn("end_turn")]
        handle, _ = await start_chat(env)

        async def taken() -> list[InternalTurnInput]:
            return script.named("internal_turn")

        await eventually(taken, lambda turns: len(turns) == 1, "the first turn")
        closed = NetworkUpdate(request_id=request_id, kind="closed")
        await handle.signal("network_update", closed)
        turns = await eventually(taken, lambda turns: len(turns) == 2, "the closed turn")
        assert (turns[1].messages, turns[1].network_updates) == ([], [closed])
        # A repeat of round 1 is dropped; the round a reopen started wakes Claude again.
        again = NetworkUpdate(request_id=request_id, kind="closed", round=2)
        await handle.signal("network_update", closed)
        await handle.signal("network_update", again)
        turns = await eventually(taken, lambda turns: len(turns) == 3, "the round 2 turn")
        assert turns[2].network_updates == [again]
        assert (await handle.result()).turns == 3
        await replays(env, handle)


async def test_requests_closed_before_rounds_count_as_round_1(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        request_id = uuid4()
        script.internal = [_turn("end_turn"), _turn("end_turn")]
        handle, _ = await start_chat(
            env, carried=ConversationCarry(turns=3, closed_request_ids=[request_id])
        )
        await eventually(taken(script), lambda turns: len(turns) == 1, "the first turn")
        again = NetworkUpdate(request_id=request_id, kind="closed", round=2)
        await handle.signal("network_update", NetworkUpdate(request_id=request_id, kind="closed"))
        await handle.signal("network_update", again)
        turns = await eventually(taken(script), lambda turns: len(turns) == 2, "round 2's turn")
        assert turns[1].network_updates == [again]
        await handle.result()
        await replays(env, handle)


async def test_a_message_during_continue_as_new_is_not_lost(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.internal = [_turn("end_turn"), _turn("end_turn"), _turn("end_turn")]
        second, third = _message("Also SEM."), _message("And EDS.")
        # The second message lands while turn 1 runs; the third races the run's
        # continue-as-new, which follows turn 2 (one turn per run, at an idle point).
        script.signal_during[1] = second
        script.signal_after[2] = third
        handle, first = await start_chat(env, turns_per_run=1)

        async def taken() -> list[InternalTurnInput]:
            return script.named("internal_turn")

        # Awaited before the result, which would let time skip past the idle exit.
        turns = await eventually(taken, lambda turns: len(turns) == 3, "the third turn")
        assert (await handle.result()).turns == 3
        assert [(t.turn, [m.text for m in t.messages]) for t in turns] == [
            (1, [first.text]),
            (2, [second.text]),
            (3, [third.text]),
        ]
        first_run = env.client.get_workflow_handle(handle.id, run_id=handle.result_run_id)
        assert (await first_run.describe()).status == WorkflowExecutionStatus.CONTINUED_AS_NEW
        await replays(env, first_run)


def taken(script: Script) -> Callable[[], Awaitable[list[InternalTurnInput]]]:
    return lambda: script.recorded("internal_turn")


def looks(script: Script, since: datetime) -> list[tuple[int, float]]:
    """Each update look as (its number, seconds after `since` it was scheduled)."""
    turns: list[InternalTurnInput] = script.named("internal_turn")
    return [
        (turn.turn, round((at - since).total_seconds()))
        for turn, at in zip(turns, script.internal_at, strict=True)
        if turn.look
    ]


async def test_an_update_look_waits_for_a_lull_and_comes_at_most_once_a_minute(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        news = NetworkNews(request_id=uuid4())
        # The first look finds nothing new, so the next one keeps its number.
        script.internal = [
            _turn("end_turn"),
            _turn("end_turn", skipped=True),
            _turn("end_turn"),
            _turn("end_turn"),
        ]
        handle, _ = await start_chat(env)
        await eventually(taken(script), lambda t: len(t) == 1, "the first turn")
        start = await env.get_current_time()
        await handle.signal("network_news", news)
        await env.sleep(timedelta(seconds=20))
        await handle.signal("network_news", news)
        await env.sleep(timedelta(seconds=40))
        await eventually(taken(script), lambda t: len(t) == 2, "the first look")
        # News a minute in: the lull ends at 90s, but the last look was at 50s.
        await handle.signal("network_news", news)
        await env.sleep(timedelta(seconds=60))
        await eventually(taken(script), lambda t: len(t) == 3, "the second look")
        await handle.signal("member_message", _message("Any news?"))
        await eventually(taken(script), lambda t: len(t) == 4, "the member's turn")

        assert (await handle.result()).turns == 2
        turns: list[InternalTurnInput] = script.named("internal_turn")
        # Looks never take a chat turn number.
        assert [(t.look, t.turn) for t in turns] == [(False, 1), (True, 1), (True, 1), (False, 2)]
        assert looks(script, start) == [(1, 50), (1, 110)]
        assert script.looking == [True, True]
        assert not (await pending(handle)).looking
        await replays(env, handle)


async def test_news_that_never_lets_up_gets_a_look_three_minutes_after_the_first(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        news = NetworkNews(request_id=uuid4())
        script.internal = [_turn("end_turn"), _turn("end_turn")]
        handle, _ = await start_chat(env)
        await eventually(taken(script), lambda t: len(t) == 1, "the first turn")
        start = await env.get_current_time()
        # News every 20 seconds, the last at 160s: never a 30-second lull.
        for _ in range(9):
            await handle.signal("network_news", news)
            await env.sleep(timedelta(seconds=20))
        await eventually(taken(script), lambda t: len(t) == 2, "the look")

        assert (await handle.result()).turns == 1
        assert looks(script, start) == [(1, 180)]
        await replays(env, handle)


async def test_an_armed_look_outlasts_the_idle_exit_and_a_closing_turn_disarms_it(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        news = NetworkNews(request_id=uuid4())
        script.internal = [_turn("end_turn"), _turn("end_turn"), _turn("end_turn")]
        handle, _ = await start_chat(env)
        await eventually(taken(script), lambda t: len(t) == 1, "the first turn")
        # News just before the idle exit: the run stays for its look.
        await env.sleep(CONVERSATION_IDLE - timedelta(seconds=10))
        await handle.signal("network_news", news)
        await env.sleep(timedelta(seconds=40))
        await eventually(taken(script), lambda t: len(t) == 2, "the look")
        # The closing turn reads every conversation, so the news before it arms nothing.
        await handle.signal("network_news", news)
        await handle.signal(
            "network_update", NetworkUpdate(request_id=news.request_id, kind="closed")
        )

        assert (await handle.result()).turns == 2
        turns: list[InternalTurnInput] = script.named("internal_turn")
        assert [(t.look, t.turn) for t in turns] == [(False, 1), (True, 1), (False, 2)]
        await replays(env, handle)


def awaiting(card_id: UUID) -> InternalTurnResult:
    return _turn("awaiting_card", card_id=card_id, card_tool_use_id="toolu_card")


async def test_a_message_sent_before_the_card_was_raised_waits_for_the_next_turn(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        card_id = uuid4()
        script.internal = [awaiting(card_id), _turn("end_turn"), _turn("end_turn")]
        # Typed while turn 1 runs, before it raises the card: the API said "queued".
        early = _message("Also SEM.")
        script.signal_during[1] = early
        handle, _ = await start_chat(env)
        await eventually(
            lambda: pending(handle),
            lambda p: p.pending_card_id == card_id and p.queued_messages == [early],
            "the card, with the message still queued",
        )
        assert script.named("apply_card_resolution") == []

        await handle.execute_update(
            "resolve_card",
            CardResolution(card_id=card_id, decision="reject", member_id=ALEX, feedback="No."),
            result_type=CardResolved,
        )
        turns = await eventually(taken(script), lambda t: len(t) == 3, "the message's turn")
        assert (await handle.result()).turns == 2
        _, resumed, after = turns
        assert resumed.card is not None
        assert resumed.card.content == "rejected: No."
        assert (after.turn, after.messages) == (2, [early])


async def test_a_card_settled_elsewhere_resumes_with_what_it_settled_to(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        card_id, request_id = uuid4(), uuid4()
        script.internal = [awaiting(card_id), _turn("end_turn"), _turn("end_turn")]
        script.card_errors = [
            ApplicationError("card is not pending", type=CARD_NOT_PENDING_ERROR, non_retryable=True)
        ]
        approved = f"approved; request {request_id} sent to Northlake"
        script.settled = CardResolutionApplied(
            resolved=CardResolved(card_id=card_id, status="approved", request_id=request_id),
            tool_use_id="toolu_card",
            content=approved,
        )
        handle, _ = await start_chat(env)
        await eventually(
            lambda: pending(handle), lambda p: p.pending_card_id == card_id, "the card"
        )
        await handle.signal("member_message", _message("Leave Calder out.", card_id))
        turns = await eventually(taken(script), lambda t: len(t) == 3, "the message's turn")
        assert (await handle.result()).turns == 2

        (asked,) = script.named("settled_card")
        assert asked.card_id == card_id
        _, resumed, after = turns
        # Claude hears the approval that happened, not a rejection that didn't.
        assert resumed.card == CardOutcome(
            card_id=card_id, tool_use_id="toolu_card", content=approved
        )
        assert [m.text for m in after.messages] == ["Leave Calder out."]


async def test_a_rejection_that_cant_be_stored_is_never_reported_as_made(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        card_id = uuid4()
        script.internal = [awaiting(card_id), _turn("end_turn")]
        script.card_errors = [
            ApplicationError("the world is retired", type=WORLD_RETIRED, non_retryable=True)
        ]
        handle, _ = await start_chat(env)
        await eventually(
            lambda: pending(handle), lambda p: p.pending_card_id == card_id, "the card"
        )
        await handle.signal("member_message", _message("Leave Calder out.", card_id))
        with pytest.raises(WorkflowFailureError) as failed:
            await handle.result()

        cause = failed.value.cause
        assert isinstance(cause, ApplicationError)
        assert cause.type == WORLD_RETIRED
        assert len(script.named("internal_turn")) == 1
        assert script.named("settled_card") == []


async def test_an_approved_request_whose_start_fails_still_starts_and_resumes(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        card_id, request_id = uuid4(), uuid4()
        script.internal = [awaiting(card_id), _turn("end_turn")]
        approved = f"approved; request {request_id} sent to Northlake"
        script.card_applied = CardResolutionApplied(
            resolved=CardResolved(card_id=card_id, status="approved", request_id=request_id),
            tool_use_id="toolu_card",
            content=approved,
        )
        script.start_failures = 2
        handle, _ = await start_chat(env)
        await eventually(
            lambda: pending(handle), lambda p: p.pending_card_id == card_id, "the card"
        )
        # The approval is stored, so the member hears it was made whatever the start does.
        resolved = await handle.execute_update(
            "resolve_card",
            CardResolution(card_id=card_id, decision="approve", member_id=ALEX),
            result_type=CardResolved,
        )
        assert resolved.status == "approved"

        assert (await handle.result()).turns == 1
        started: list[StartRequestInput] = script.named("start_network_request")
        assert [s.request_id for s in started] == [request_id] * 3
        _, resumed = script.named("internal_turn")
        assert resumed.card == CardOutcome(
            card_id=card_id, tool_use_id="toolu_card", content=approved
        )
        await replays(env, handle)


async def test_an_approval_that_failed_after_its_commit_is_read_back_and_resumes(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        card_id, request_id = uuid4(), uuid4()
        script.internal = [awaiting(card_id), _turn("end_turn")]
        script.card_errors = [ApplicationError("connection lost", non_retryable=True)]
        approved = f"approved; request {request_id} sent to Northlake"
        script.settled = CardResolutionApplied(
            resolved=CardResolved(card_id=card_id, status="approved", request_id=request_id),
            tool_use_id="toolu_card",
            content=approved,
        )
        handle, _ = await start_chat(env)
        await eventually(
            lambda: pending(handle), lambda p: p.pending_card_id == card_id, "the card"
        )
        await update_error(
            handle.execute_update(
                "resolve_card",
                CardResolution(card_id=card_id, decision="approve", member_id=ALEX),
            )
        )

        assert (await handle.result()).turns == 1
        (asked,) = script.named("settled_card")
        assert asked.card_id == card_id
        started: list[StartRequestInput] = script.named("start_network_request")
        assert [s.request_id for s in started] == [request_id]
        _, resumed = script.named("internal_turn")
        assert resumed.card == CardOutcome(
            card_id=card_id, tool_use_id="toolu_card", content=approved
        )
        await replays(env, handle)


async def test_an_approval_that_failed_before_its_commit_leaves_the_card_pending(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        card_id = uuid4()
        script.internal = [awaiting(card_id), _turn("end_turn")]
        script.card_errors = [ApplicationError("the database is down", non_retryable=True)]
        handle, _ = await start_chat(env)
        await eventually(
            lambda: pending(handle), lambda p: p.pending_card_id == card_id, "the card"
        )
        await update_error(
            handle.execute_update(
                "resolve_card",
                CardResolution(card_id=card_id, decision="approve", member_id=ALEX),
            )
        )
        await eventually(
            lambda: script.recorded("settled_card"), lambda r: len(r) == 1, "the read-back"
        )
        assert len(script.named("internal_turn")) == 1

        resolved = await retried_update(
            lambda: handle.execute_update(
                "resolve_card",
                CardResolution(card_id=card_id, decision="reject", member_id=ALEX, feedback="No."),
                result_type=CardResolved,
            )
        )
        assert resolved.status == "rejected"
        assert (await handle.result()).turns == 1
        _, resumed = script.named("internal_turn")
        assert resumed.card is not None
        assert resumed.card.content == "rejected: No."
        await replays(env, handle)


async def test_a_repeated_closed_notice_wakes_claude_once_even_across_runs(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        request_id = uuid4()
        script.internal = [_turn("end_turn"), _turn("end_turn"), _turn("end_turn")]
        # One turn per run: every turn is followed by a continue-as-new.
        handle, _ = await start_chat(env, turns_per_run=1)

        async def run_after(run_id: str | None) -> str | None:
            described = await eventually(
                handle.describe, lambda d: d.run_id != run_id, "the next run"
            )
            return described.run_id

        await eventually(taken(script), lambda t: len(t) == 1, "the first turn")
        second_run = await run_after(handle.first_execution_run_id)
        closed = NetworkUpdate(request_id=request_id, kind="closed")
        await handle.signal("network_update", closed)
        await eventually(taken(script), lambda t: len(t) == 2, "the closed turn")
        await run_after(second_run)
        # Delivered again (a retried notice), in the run the first one was carried into.
        await handle.signal("network_update", closed)
        await handle.signal("member_message", _message("Thanks."))
        turns = await eventually(taken(script), lambda t: len(t) == 3, "the message's turn")

        assert [t.network_updates for t in turns] == [[], [closed], []]
        assert [m.text for m in turns[2].messages] == ["Thanks."]
        assert (await pending(handle)).network_updates == []
        assert (await handle.result()).turns == 3


@pytest.mark.parametrize(
    "lost",
    [ApplicationError("overloaded", non_retryable=True), _turn("refusal")],
    ids=["failed", "refused"],
)
async def test_a_closed_note_its_turn_never_stored_goes_with_the_next_turn(
    temporal_env_time_skipping: WorkflowEnvironment,
    lost: InternalTurnResult | ApplicationError,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        request_id = uuid4()
        script.internal = [_turn("end_turn"), lost, _turn("end_turn")]
        handle, _ = await start_chat(env)
        await eventually(taken(script), lambda t: len(t) == 1, "the first turn")
        closed = NetworkUpdate(request_id=request_id, kind="closed")
        await handle.signal("network_update", closed)
        await eventually(taken(script), lambda t: len(t) == 2, "the closed turn")
        await eventually(
            lambda: pending(handle), lambda p: not p.turn_in_flight, "the closed turn's end"
        )
        # Past the idle timeout: the owed note keeps the run open, and wakes nothing.
        await env.sleep(CONVERSATION_IDLE * 2)
        assert len(script.named("internal_turn")) == 2
        # A repeated notice is still dropped; the member's next turn carries the note.
        await handle.signal("network_update", closed)
        await handle.signal("member_message", _message("How did it go?"))
        turns = await eventually(taken(script), lambda t: len(t) == 3, "the member's turn")

        assert [t.network_updates for t in turns] == [[], [closed], [closed]]
        assert [m.text for m in turns[2].messages] == ["How did it go?"]
        assert (await handle.result()).turns == 3
        await replays(env, handle)


async def test_a_failed_turn_is_reported_until_the_next_one(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.internal = [
            ApplicationError("no scripted turn", non_retryable=True),
            _turn("end_turn"),
        ]
        handle, _ = await start_chat(env, turns_per_run=1)
        await eventually(taken(script), lambda t: len(t) == 1, "the failed turn")
        # Still reported after the run continues as new.
        await eventually(
            handle.describe,
            lambda d: d.run_id != handle.first_execution_run_id,
            "the next run",
        )
        failed = await pending(handle)
        assert (failed.turns, failed.turn_failure) == (1, "model_error")

        await handle.signal("member_message", _message("Try again."))
        await eventually(taken(script), lambda t: len(t) == 2, "the next turn")
        retried = await eventually(
            lambda: pending(handle), lambda p: not p.turn_in_flight, "the next turn's end"
        )
        assert (retried.turns, retried.turn_failure) == (2, None)
        assert (await handle.result()).turns == 2


def auto_card(card_id: UUID) -> InternalTurnResult:
    """A turn paused on a card its chat sends without review."""
    return _turn("awaiting_card", card_id=card_id, card_tool_use_id="toolu_card", card_auto=True)


def updates_in(history: Any) -> int:
    return sum(
        1
        for e in history.events
        if e.HasField("workflow_execution_update_accepted_event_attributes")
    )


def scheduled_retries(history: Any, activity_name: str) -> list[int]:
    """The `maximum_attempts` each scheduling of `activity_name` asked for."""
    return [
        e.activity_task_scheduled_event_attributes.retry_policy.maximum_attempts
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
        and e.activity_task_scheduled_event_attributes.activity_type.name == activity_name
    ]


CHAT_FAILURES = [
    *(pytest.param([error] * attempts(error), reason, id=name) for name, error, reason in FAILURES),
    pytest.param([_turn("refusal")], "refused", id="refusal"),
]


@pytest.mark.parametrize(("lost", "reason"), CHAT_FAILURES)
async def test_why_a_turn_failed_is_shown_until_the_next_message(
    temporal_env_time_skipping: WorkflowEnvironment,
    lost: list[InternalTurnResult | ApplicationError],
    reason: FailureReason,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.internal = [*lost, _turn("end_turn")]
        handle, _ = await start_chat(env)
        if len(lost) > 1:
            # Let the server's retry timers run out.
            await env.sleep(timedelta(minutes=5))
        failed = await eventually(
            lambda: pending(handle),
            lambda p: p.turn_failure is not None and not p.turn_in_flight,
            "the failed turn",
        )
        assert failed.turn_failure == reason
        assert len(script.named("internal_turn")) == len(lost)

        await handle.signal("member_message", _message("Try again."))
        retried = await eventually(
            lambda: pending(handle),
            lambda p: p.turns == 2 and not p.turn_in_flight,
            "the next turn's end",
        )
        assert retried.turn_failure is None
        assert (await handle.result()).turns == 2


async def test_an_auto_card_is_sent_as_its_owner_without_an_update(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        card_id, request_id = uuid4(), uuid4()
        script.internal = [auto_card(card_id), _turn("end_turn")]
        script.card_requests[card_id] = request_id
        script.gates["apply_card_resolution"] = asyncio.Event()
        script.gates["start_network_request"] = asyncio.Event()
        handle, _ = await start_chat(env)
        await eventually(
            lambda: script.recorded("apply_card_resolution"), lambda r: len(r) == 1, "the send"
        )
        # Never the member's to settle: no pending card, and resolving it is refused.
        # The chat reads busy all along, so the web keeps following the turn.
        assert ((p := await pending(handle)).pending_card_id, p.turn_in_flight) == (None, True)
        cause = await update_error(
            handle.execute_update(
                "resolve_card",
                CardResolution(card_id=card_id, decision="reject", member_id=ALEX, feedback="No"),
            )
        )
        assert cause.type == CARD_NOT_PENDING_ERROR
        script.gates["apply_card_resolution"].set()
        await eventually(
            lambda: script.recorded("start_network_request"), lambda r: len(r) == 1, "the start"
        )
        assert (await pending(handle)).turn_in_flight
        script.gates["start_network_request"].set()
        assert (await handle.result()).turns == 1

        assert [name for name, _ in script.calls] == [
            "internal_turn",
            "apply_card_resolution",
            "start_network_request",
            "internal_turn",
        ]
        (applied,) = script.named("apply_card_resolution")
        assert applied.resolution == CardResolution(
            card_id=card_id, decision="approve", member_id=ALEX, auto=True
        )
        started: list[StartRequestInput] = script.named("start_network_request")
        assert [s.request_id for s in started] == [request_id]
        _, resumed = script.named("internal_turn")
        assert resumed.card == CardOutcome(
            card_id=card_id, tool_use_id="toolu_card", content=f"approved; request {request_id}"
        )
        history = await handle.fetch_history()
        assert updates_in(history) == 0
        # Mirror options: retried until it is stored.
        assert scheduled_retries(history, "apply_card_resolution") == [0]
        await replays(env, handle)


async def test_an_auto_send_that_fails_once_starts_its_request_once(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        card_id, request_id = uuid4(), uuid4()
        script.internal = [auto_card(card_id), _turn("end_turn")]
        script.card_requests[card_id] = request_id
        script.card_errors = [ApplicationError("the database is down")]
        handle, _ = await start_chat(env)
        assert (await handle.result()).turns == 1

        applied: list[CardResolutionInput] = script.named("apply_card_resolution")
        assert [a.resolution.decision for a in applied] == ["approve", "approve"]
        started: list[StartRequestInput] = script.named("start_network_request")
        assert [s.request_id for s in started] == [request_id]
        await replays(env, handle)


async def test_an_auto_send_that_cant_be_stored_rejects_the_card_and_resumes(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        card_id = uuid4()
        script.internal = [auto_card(card_id), _turn("end_turn"), _turn("end_turn")]
        script.card_requests[card_id] = uuid4()
        script.card_errors = [
            ApplicationError(
                "no recipient is left to send to", type=INVALID_RESOLUTION_ERROR, non_retryable=True
            )
        ]
        handle, _ = await start_chat(env)
        await eventually(taken(script), lambda t: len(t) == 2, "the resumed turn")
        await handle.signal("member_message", _message("Why not?"))
        turns = await eventually(taken(script), lambda t: len(t) == 3, "the member's turn")
        assert (await handle.result()).turns == 2

        approve, reject = script.named("apply_card_resolution")
        assert approve.resolution.decision == "approve"
        assert reject.resolution == CardResolution(
            card_id=card_id,
            decision="reject",
            member_id=ALEX,
            feedback="no recipient is left to send to",
            auto=True,
        )
        assert script.named("start_network_request") == []
        _, resumed, after = turns
        assert resumed.card is not None
        assert resumed.card.content == "rejected: no recipient is left to send to"
        assert (after.turn, [m.text for m in after.messages]) == (2, ["Why not?"])
        await replays(env, handle)


async def test_two_auto_sends_in_one_turn_create_two_requests(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        first, second = uuid4(), uuid4()
        script.card_requests = {first: uuid4(), second: uuid4()}
        script.internal = [auto_card(first), auto_card(second), _turn("end_turn")]
        handle, _ = await start_chat(env)
        assert (await handle.result()).turns == 1

        started: list[StartRequestInput] = script.named("start_network_request")
        assert [s.request_id for s in started] == [
            script.card_requests[first],
            script.card_requests[second],
        ]
        turns: list[InternalTurnInput] = script.named("internal_turn")
        assert [t.card.card_id if t.card else None for t in turns] == [None, first, second]
        assert len({t.turn_id for t in turns}) == 1, "one turn, resumed twice"
        assert updates_in(await handle.fetch_history()) == 0
        await replays(env, handle)


# --- NetworkRequestWorkflow -----------------------------------------------------------


async def start_request(
    env: WorkflowEnvironment,
    recipients: list[UUID],
    *,
    world_id: UUID | None = None,
    **overrides: Any,
) -> tuple[WorkflowHandle[Any, Any], UUID]:
    world_id, request_id = world_id or uuid4(), uuid4()
    handle = await env.client.start_workflow(
        NetworkRequestWorkflow.run,
        NetworkRequestInput.model_validate(
            {
                "world_id": world_id,
                "request_id": request_id,
                "requester_org_id": HENDERSON,
                "conversation_id": None,
                "recipient_org_ids": recipients,
                **overrides,
            }
        ),
        id=request_workflow_id(world_id, request_id),
        task_queue=MAIN_TASK_QUEUE,
    )
    return handle, world_id


async def request_status(handle: WorkflowHandle[Any, Any]) -> RequestProgress:
    return await handle.query("status", result_type=RequestProgress)


async def test_a_request_opens_threads_in_a_window_and_closes_once_all_have_ended(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        conversation_id = uuid4()
        handle, world_id = await start_request(
            env, [NORTHLAKE, CALDER], conversation_id=conversation_id, max_active_threads=1
        )

        def thread_count(count: int) -> Callable[[RequestProgress], bool]:
            return lambda status: len(status.threads) == count

        # One slot: Calder waits until Northlake's thread has ended.
        first = await eventually(
            lambda: request_status(handle), thread_count(1), "the first thread"
        )
        northlake = first.threads[0].thread_id
        await handle.signal(
            "thread_changed",
            ThreadChanged(
                thread_id=northlake, responder_org_id=NORTHLAKE, state="awaiting_principal"
            ),
        )
        waiting = await eventually(
            lambda: request_status(handle),
            lambda s: s.threads[0].state == "awaiting_principal",
            "Northlake waiting on its principal",
        )
        assert (waiting.status, len(waiting.threads)) == ("active", 1)
        await handle.signal(
            "thread_changed",
            ThreadChanged(thread_id=northlake, responder_org_id=NORTHLAKE, state="closed"),
        )
        both = await eventually(lambda: request_status(handle), thread_count(2), "Calder's")
        calder = next(t.thread_id for t in both.threads if t.responder_org_id == CALDER)
        await handle.signal(
            "thread_changed",
            ThreadChanged(thread_id=calder, responder_org_id=CALDER, state="expired"),
        )
        result = await handle.result()

        assert result.status == "closed"
        opened: list[OpenThreadInput] = script.named("open_thread")
        assert [o.responder_org_id for o in opened] == [NORTHLAKE, CALDER]
        rounds: list[EndRoundInput] = script.named("end_round")
        assert [r.from_workflow for r in rounds] == [True]
        notes: list[NotifyConversationInput] = script.named("notify_conversation")
        assert [(n.conversation_id, n.update.kind, n.update.round) for n in notes] == [
            (conversation_id, "closed", 1)
        ]
        final = await request_status(handle)
        assert [(t.responder_org_id, t.state) for t in final.threads] == [
            (NORTHLAKE, "closed"),
            (CALDER, "expired"),
        ]
        for thread_id in (northlake, calder):
            await stop(env.client.get_workflow_handle(thread_workflow_id(world_id, thread_id)))
        await replays(env, handle)


async def test_an_ended_thread_stays_ended_and_a_request_without_a_chat_tells_nobody(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        handle, world_id = await start_request(env, [NORTHLAKE, CALDER])
        both = await eventually(
            lambda: request_status(handle), lambda s: len(s.threads) == 2, "both threads"
        )
        northlake, calder = (
            next(t.thread_id for t in both.threads if t.responder_org_id == org)
            for org in (NORTHLAKE, CALDER)
        )
        await handle.signal(
            "thread_changed",
            ThreadChanged(thread_id=northlake, responder_org_id=NORTHLAKE, state="closed"),
        )
        # A late signal from before the end doesn't bring the thread back.
        await handle.signal(
            "thread_changed",
            ThreadChanged(thread_id=northlake, responder_org_id=NORTHLAKE, state="active"),
        )
        await eventually(
            lambda: request_status(handle),
            lambda s: s.threads[0].state == "closed",
            "Northlake closed",
        )
        await handle.signal(
            "thread_changed",
            ThreadChanged(thread_id=calder, responder_org_id=CALDER, state="closed"),
        )
        assert (await handle.result()).status == "closed"
        assert script.named("notify_conversation") == []
        for thread_id in (northlake, calder):
            await stop(env.client.get_workflow_handle(thread_workflow_id(world_id, thread_id)))


async def test_a_request_with_a_reopened_thread_live_ends_without_closing(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        # A thread was reopened before the workflow's tail: its own run closes the request.
        script.round_ends = [RoundEnded(closed=False, round=2, conversation_id=None)]
        handle, world_id = await start_request(env, [NORTHLAKE], conversation_id=uuid4())
        (opened,) = (
            await eventually(
                lambda: request_status(handle), lambda s: len(s.threads) == 1, "the thread"
            )
        ).threads
        await handle.signal(
            "thread_changed",
            ThreadChanged(thread_id=opened.thread_id, responder_org_id=NORTHLAKE, state="closed"),
        )

        assert (await handle.result()).status == "active"
        assert [r.from_workflow for r in script.named("end_round")] == [True]
        assert script.named("notify_conversation") == []
        await stop(env.client.get_workflow_handle(thread_workflow_id(world_id, opened.thread_id)))
        await replays(env, handle)


async def test_a_request_whose_threads_all_stop_on_the_budget_closes_and_wakes_claude_once(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.real_threads = script.forward_notes = True
        script.rep_failure = refused("global")
        script.internal = [_turn("end_turn"), _turn("end_turn")]
        chat, _ = await start_chat(env)
        (first,) = await eventually(taken(script), lambda t: len(t) == 1, "the first turn")
        handle, _ = await start_request(
            env,
            [NORTHLAKE, CALDER],
            world_id=first.world_id,
            conversation_id=first.conversation_id,
        )
        result = await handle.result()

        assert result.status == "closed"
        assert sorted(stop_reasons(script)) == [("closed", "demo_budget")] * 2
        notes: list[NotifyConversationInput] = script.named("notify_conversation")
        assert [n.update.kind for n in notes] == ["closed"]
        turns = await eventually(taken(script), lambda t: len(t) == 2, "the closed turn")
        assert [u.kind for u in turns[1].network_updates] == ["closed"]
        assert (await chat.result()).turns == 2
        assert len(script.named("internal_turn")) == 2
        await replays(env, handle, chat)
