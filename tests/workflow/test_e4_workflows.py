"""The network workflows on the time-skipping test server, with scripted stand-ins for
their activities (§15): the thread's turn loop, close and the other side's last turn,
consult escalations and their Updates, messages held for a teammate, the turn cap and
the deadline, the conversation's card pause, wake-ups and continue-as-new, and the
request's sliding window.

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

from harmony.agents.models import CardResolution, EscalationAction, EscalationResolution
from harmony.agents.runtime import (
    ConsultOpened,
    MessageHeld,
    MessageSent,
    OwedResult,
    TurnEffect,
    TurnStop,
)
from harmony.db.models.choices import ThreadMessageKind
from harmony.ids import turn_id_for
from harmony.temporal.activities.agents import (
    AnswerConsultInput,
    CardOutcome,
    ConsultDecision,
    ConsultEscalated,
    ConsultReleased,
    InternalTurnInput,
    InternalTurnResult,
    RepTurnInput,
    RepTurnResult,
)
from harmony.temporal.activities.network import (
    ApplyResolutionInput,
    CardResolutionApplied,
    CardResolutionInput,
    NotifyConversationInput,
    OpenThreadInput,
    ReassignInput,
    RedraftFailedInput,
    RequestStatusInput,
    ResolutionApplied,
    SettledCardInput,
    StartRequestInput,
    ThreadStateInput,
)
from harmony.temporal.policies import MAIN_TASK_QUEUE, MODEL_TASK_QUEUE, WORLD_RETIRED
from harmony.temporal.workflows.conversation import ConversationWorkflow
from harmony.temporal.workflows.network_request import NetworkRequestWorkflow
from harmony.temporal.workflows.payloads import (
    CARD_NOT_PENDING_ERROR,
    CONVERSATION_IDLE,
    ESCALATION_NOT_OPEN_ERROR,
    INVALID_RESOLUTION_ERROR,
    NOT_IN_ORG_ERROR,
    CardResolved,
    ConversationInput,
    ConversationPending,
    EscalationResolved,
    MemberMessage,
    NetworkRequestInput,
    NetworkUpdate,
    Reassigned,
    Reassignment,
    Redraft,
    RedraftStarted,
    RequestProgress,
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
        # A failure in place of a decision fails that call.
        self.consults: list[ConsultDecision | ApplicationError] = []
        # A failure in place of a result fails that turn.
        self.internal: list[InternalTurnResult | ApplicationError] = []
        self.applied: ResolutionApplied | None = None
        # Raised by `apply_resolution` in place of a result, one per call.
        self.apply_errors: list[ApplicationError] = []
        # What `stored_resolution` reads back after a failed apply.
        self.stored: ResolutionApplied | None = None
        self.card_applied: CardResolutionApplied | None = None
        # Raised by `apply_card_resolution` before it stores anything, one per call.
        self.card_errors: list[ApplicationError] = []
        self.settled: CardResolutionApplied | None = None
        # Transient failures `set_thread_state` raises before it succeeds.
        self.state_failures = 0
        # Transient failures `start_network_request` raises before it succeeds.
        self.start_failures = 0
        # An activity named here waits for its event before it answers.
        self.gates: dict[str, asyncio.Event] = {}
        # Messages `internal_turn` signals to its own chat, by turn: before it returns,
        # or just after (racing the run's continue-as-new).
        self.signal_during: dict[int, MemberMessage] = {}
        self.signal_after: dict[int, MemberMessage] = {}
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

    @activity.defn(name="internal_turn")
    async def internal_turn(self, turn: InternalTurnInput) -> InternalTurnResult:
        self.calls.append(("internal_turn", turn))
        chat = self.client.get_workflow_handle(
            conversation_workflow_id(turn.world_id, turn.conversation_id)
        )
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

    @activity.defn(name="notify_conversation")
    async def notify_conversation(self, notify: NotifyConversationInput) -> None:
        self.calls.append(("notify_conversation", notify))

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

    @activity.defn(name="report_redraft_failed")
    async def report_redraft_failed(self, failed: RedraftFailedInput) -> None:
        self.calls.append(("report_redraft_failed", failed))
        await self._gate("report_redraft_failed")

    @activity.defn(name="reassign_escalation")
    async def reassign_escalation(self, reassign: ReassignInput) -> Reassigned:
        self.calls.append(("reassign_escalation", reassign))
        change = reassign.reassignment
        return Reassigned(escalation_id=change.escalation_id, assignees=[change.to_member_id])

    @activity.defn(name="apply_card_resolution")
    async def apply_card_resolution(self, card: CardResolutionInput) -> CardResolutionApplied:
        self.calls.append(("apply_card_resolution", card))
        if self.card_errors:
            raise self.card_errors.pop(0)
        resolution = card.resolution
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
        if self.start_failures > 0:
            self.start_failures -= 1
            raise ApplicationError("the database is down")

    @activity.defn(name="open_thread")
    async def open_thread(self, opened: OpenThreadInput) -> None:
        self.calls.append(("open_thread", opened))
        await self.client.start_workflow(
            FakeThread.run,
            id=thread_workflow_id(opened.world_id, opened.thread_id),
            task_queue=MAIN_TASK_QUEUE,
        )

    @activity.defn(name="set_request_status")
    async def set_request_status(self, status: RequestStatusInput) -> None:
        self.calls.append(("set_request_status", status))


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
            script.notify_conversation,
            script.apply_resolution,
            script.stored_resolution,
            script.reassign_escalation,
            script.report_redraft_failed,
            script.apply_card_resolution,
            script.settled_card,
            script.start_network_request,
            script.open_thread,
            script.set_request_status,
        ],
    )
    model = Worker(
        env.client,
        task_queue=MODEL_TASK_QUEUE,
        activities=[script.rep_turn, script.answer_consult, script.internal_turn],
    )
    async with main, model:
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
        # The request hears the end once, after the last turn.
        assert await request_saw(request) == ["closed"]
        await stop(request)
        await replays(env, handle)


async def test_a_turn_that_sent_nothing_still_hands_the_move_over(
    temporal_env_time_skipping: WorkflowEnvironment,
) -> None:
    env = temporal_env_time_skipping
    async with workers(env) as script:
        script.rep[("requester", 1)] = Plan()
        late = OwedResult(tool_use_id="toolu_late", content="Not run: the turn ran out of steps.")
        script.rep[("responder", 1)] = Plan(stop="max_steps", owed=late)
        script.rep[("requester", 2)] = said("close")
        script.rep[("responder", 2)] = Plan()
        _, handle, request = await start_thread(env)
        result = await handle.result()

        assert (result.state, result.turns) == ("closed", 4)
        assert states(script) == [
            ("active", "responder", None),
            ("active", "requester", None),
            ("closed", None, "requester"),
        ]
        last: RepTurnInput = script.named("rep_turn")[-1]
        assert (last.side, last.owed) == ("responder", [late])
        await stop(request)


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
        assert script.named("answer_consult") == []
        assert await request_saw(request) == ["closed"]
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
        started = await handle.execute_update(
            "redraft",
            Redraft(escalation_id=escalation_id, instruction="Shorter.", by_member_id=HELEN),
            result_type=RedraftStarted,
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


async def test_an_approval_applies_the_rev_it_names_whatever_was_redrafted(
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
        await handle.execute_update(
            "redraft",
            Redraft(escalation_id=escalation_id, instruction="Use the block.", by_member_id=RAJ),
            result_type=RedraftStarted,
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


async def test_a_redraft_that_writes_no_draft_is_reported_so_nobody_waits_on_it(
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
            # The model call fails (a scripted run has no cassette for a redraft).
            ApplicationError("no cassette entry", non_retryable=True),
            # The answerer returned no draft.
            escalated(consult_id, escalation_id, None),
            escalated(consult_id, escalation_id, 4),
        ]
        _, handle, request = await start_thread(env)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [escalation_id],
            "the escalation",
        )

        async def redraft(instruction: str, calls: int) -> int:
            started = await handle.execute_update(
                "redraft",
                Redraft(escalation_id=escalation_id, instruction=instruction, by_member_id=RAJ),
                result_type=RedraftStarted,
            )
            await eventually(
                lambda: script.recorded("answer_consult"), lambda c: len(c) == calls, instruction
            )
            return started.draft_rev

        def reported() -> list[tuple[UUID, int]]:
            failed: list[RedraftFailedInput] = script.named("report_redraft_failed")
            return [(f.escalation_id, f.draft_rev) for f in failed]

        assert await redraft("Shorter.", 2) == 2
        await eventually(lambda: asyncio.sleep(0, reported()), lambda r: len(r) == 1, "rev 2")
        assert await redraft("Friendlier.", 3) == 3
        await eventually(lambda: asyncio.sleep(0, reported()), lambda r: len(r) == 2, "rev 3")
        assert await redraft("Mention Nov 4.", 4) == 4
        script.applied = ResolutionApplied(
            resolved=EscalationResolved(escalation_id=escalation_id, action="approve"),
            owed=OwedResult(tool_use_id="", content="We can do Nov 4."),
            side="responder",
        )
        await handle.execute_update(
            "resolve_escalation",
            resolution(escalation_id, "approve", RAJ, NORTHLAKE, draft_rev=4),
            result_type=EscalationResolved,
        )

        assert (await handle.result()).state == "closed"
        # Rev 4 was written, so only the two that weren't are reported.
        assert reported() == [(escalation_id, 2), (escalation_id, 3)]
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
        assert (await progress(handle)).open_escalation_ids == []
        await stop(request)
        await replays(env, handle)


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


async def test_a_redraft_stops_when_its_item_is_resolved(
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
        # The redraft's model call doesn't answer.
        script.gates["answer_consult"] = asyncio.Event()
        await handle.execute_update(
            "redraft",
            Redraft(escalation_id=escalation_id, instruction="Shorter.", by_member_id=RAJ),
            result_type=RedraftStarted,
        )
        await eventually(
            lambda: script.recorded("answer_consult"), lambda calls: len(calls) == 2, "the redraft"
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
        # Stopped, not failed: nobody waits on the rev of a resolved item.
        assert script.named("report_redraft_failed") == []
        await stop(request)
        await replays(env, handle)


async def test_a_redraft_stops_when_the_thread_ends(
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
        await handle.execute_update(
            "redraft",
            Redraft(escalation_id=escalation_id, instruction="Shorter.", by_member_id=RAJ),
            result_type=RedraftStarted,
        )
        await eventually(
            lambda: script.recorded("answer_consult"), lambda calls: len(calls) == 2, "the redraft"
        )

        # Nobody decides and the deadline passes with the redraft still drafting.
        assert (await handle.result()).state == "expired"
        assert await cancelled_activities(handle) == ["answer_consult"]
        assert script.named("report_redraft_failed") == []
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
        script.consults = [
            escalated(consult_id, escalation_id, 1),
            ApplicationError("overloaded", non_retryable=True),
        ]
        deadline = (await env.get_current_time()) + timedelta(seconds=15)
        _, handle, request = await start_thread(env, deadline_at=deadline)
        await eventually(
            lambda: progress(handle),
            lambda p: p.open_escalation_ids == [escalation_id],
            "the escalation",
        )
        # A redraft fails for real, and its report hangs on the database.
        script.gates["report_redraft_failed"] = asyncio.Event()
        await handle.execute_update(
            "redraft",
            Redraft(escalation_id=escalation_id, instruction="Shorter.", by_member_id=RAJ),
            result_type=RedraftStarted,
        )
        await eventually(
            lambda: script.recorded("report_redraft_failed"), bool, "the redraft's report"
        )
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
        assert await cancelled_activities(handle) == ["report_redraft_failed", "stored_resolution"]
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
    env: WorkflowEnvironment, text: str = "Find XRD time.", *, turns_per_run: int = 50
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
        script.internal = [_turn("end_turn"), _turn("end_turn")]
        handle, _ = await start_chat(env)

        async def taken() -> list[InternalTurnInput]:
            return script.named("internal_turn")

        await eventually(taken, lambda turns: len(turns) == 1, "the first turn")
        closed = NetworkUpdate(request_id=request_id, kind="closed")
        await handle.signal("network_update", closed)
        turns = await eventually(taken, lambda turns: len(turns) == 2, "the closed turn")
        assert (turns[1].messages, turns[1].network_updates) == ([], [closed])
        assert (await handle.result()).turns == 2
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
        assert (failed.turns, failed.turn_failed) == (1, True)

        await handle.signal("member_message", _message("Try again."))
        await eventually(taken(script), lambda t: len(t) == 2, "the next turn")
        retried = await eventually(
            lambda: pending(handle), lambda p: not p.turn_in_flight, "the next turn's end"
        )
        assert (retried.turns, retried.turn_failed) == (2, False)
        assert (await handle.result()).turns == 2


# --- NetworkRequestWorkflow -----------------------------------------------------------


async def start_request(
    env: WorkflowEnvironment, recipients: list[UUID], **overrides: Any
) -> tuple[WorkflowHandle[Any, Any], UUID]:
    world_id, request_id = uuid4(), uuid4()
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
        statuses: list[RequestStatusInput] = script.named("set_request_status")
        assert [s.status for s in statuses] == ["closed"]
        notes: list[NotifyConversationInput] = script.named("notify_conversation")
        assert [(n.conversation_id, n.update.kind) for n in notes] == [(conversation_id, "closed")]
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
