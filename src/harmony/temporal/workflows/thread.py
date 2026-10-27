"""`ThreadWorkflow`: one conversation between two representatives (§5, §7).
ID `w:{world}:thread:{thread_id}`.

The requester's representative opens; then the move alternates between the sides. A
side's turn (`AgentActivities.rep_turn`) is a tool loop: it may send several messages,
and it ends when the model stops calling tools (`end_turn`), at `max_steps`, or on a
call that stops it:

- `consult`: the thread goes `awaiting_principal` and `answer_consult` runs. Released:
  the same side moves again, its next turn opening with the answer as the owed result.
  Escalated: the thread waits for a person (`resolve_escalation`); a requester-side
  question also shows inline in the request's chat, which refetches from the
  escalation's own commit (no model turn needs it);
- a message held for a teammate (*Ask*, `MessageHeld`): the thread waits
  `awaiting_principal`, and moves on as after whatever the person let through: the
  approved message (the counterparty moves next), nothing (the same side again, owed
  the refusal) or the member's own message (`take_over`);
- `close`: the thread is `closed` for both sides. The other side gets the `closed`
  notice and one last turn, in which only its own `close` (its summary) takes effect.
  If that turn fails or is refused, the close stands (with no `stop_reason`), and its
  org gets a note from Claude saying why there is no summary (`note_unsummarized`).

Otherwise the other side moves next. A refusal closes the thread as a `close` without a
summary would, the other side's failed last turn getting the same note, or, before any
message has crossed, with no last turn (the responder has nothing to answer or
summarize); a failed turn or answer closes it with no last turn.
Either way the row gets a `stop_reason` (3.5): `refused`, a spend cap's reason, or
`model_error`. The thread ends `expired` after two silent turns in a row, one per side
(no message, no consult, no close: 3.13 E3), after a requester turn that ends before any
message has crossed (its opener declined, say: the responder would have nothing to
answer, and the requester always writes first), at `max_turns` (both sides' turns
together) or at `deadline_at`, which bounds every wait, a person's included, so the
thread writes its own terminal state inside the execution timeout: these are §5's turn
cap and idle timeout. Each state change is mirrored to the row (`set_thread_state`,
which the tools read to check whose turn it is) and signalled to the request
(`thread_changed`); the terminal state only once the last turn is done, so the request
never reports a thread before both summaries could be written.

A member's Edit is the Update `steer` (docs/design/EDIT.md §3). It records the
instruction (`record_steer`); then the side's guidance is written (`write_guidance`)
and, when the member was looking at one of the side's waiting items, its draft is
rewritten as the next rev (`answer_consult` for a consult, `rewrite_held` for a held
message), both at once; the steer is settled from what landed (`settle_steer`). One
steer runs per side at a time. An Edit never starts a turn: the side's next turn waits
for its steer and opens with the new guidance. A turn that sent nothing while its side's
steer was recorded (its close refused, say) isn't silent: the move stays with that side.
A resolution is pinned to the rev the member saw, so a rewrite landing meanwhile never
changes what is sent or released.

Every end stores an `end_reason` with the terminal state (docs/design/REOPEN.md §2.1):
`closed` by a `close`, `stopped` by a failure or refusal, `quiet`, `unsent`,
`turn_limit` or `deadline`; a close or refusal also stores which side did it.

A Reopen (REOPEN.md §4) starts the thread's next run under the same id, with
`ThreadInput.reopen` and what the last run carried: turn numbers, delivered seqs,
consult ordinals and owed results continue, so the representatives' transcripts stay
valid; the run gets fresh turns and a new deadline. The thread stays ended while the
reopening side's guidance is written; only when it changed does `begin_reopen` make the
thread live again, and that side moves first. Otherwise the steer is settled failed and
the run returns, having changed nothing the other side can see. Every run's last
activity is `finish_run`, which stores its carry and lets Reopen on. Run 1 signals the
request as above; a later run closes the request itself when nothing on it is live
(`end_round`), and tells the chat.

Rep turn ids are `ids.turn_id_for(world, thread, org, side turn)`, as P0's turns are.

Owner: R-temporal.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError, FailureError

with workflow.unsafe.imports_passed_through():
    from harmony.agents.models import EscalationResolution
    from harmony.agents.runtime import (
        ConsultOpened,
        MessageHeld,
        MessageSent,
        OwedResult,
        TurnStop,
    )
    from harmony.agents.tools.rep import CUT_OFF_CONSULT, CUT_OFF_HELD
    from harmony.db.models.choices import (
        EndReason,
        EscalationKind,
        FailureReason,
        Side,
        SteerFailure,
        ThreadState,
    )
    from harmony.ids import turn_id_for
    from harmony.temporal.activities.agents import (
        AgentActivities,
        AnswerConsultInput,
        ConsultEscalated,
        RepTurnInput,
        RepTurnResult,
        SteerPartInput,
    )
    from harmony.temporal.activities.network import (
        ApplyResolutionInput,
        BeginReopenInput,
        EndRoundInput,
        FinishRunInput,
        NetworkActivities,
        NotifyConversationInput,
        ReassignInput,
        RecordReopenInput,
        RecordSteerInput,
        ResolutionApplied,
        SettleSteerInput,
        SteerRecorded,
        ThreadStateInput,
        UnsummarizedInput,
    )
    from harmony.temporal.failures import failure_reason
    from harmony.temporal.policies import (
        main_activity_options,
        mirror_activity_options,
        model_activity_options,
    )
    from harmony.temporal.workflows.payloads import (
        ESCALATION_NOT_OPEN_ERROR,
        INVALID_RESOLUTION_ERROR,
        NOT_IN_ORG_ERROR,
        STEER_CONFLICT_ERROR,
        STEER_RUNNING_ERROR,
        THREAD_CLOSED_ERROR,
        EscalationResolved,
        NetworkUpdate,
        Reassigned,
        Reassignment,
        ReopenInput,
        SteerRequest,
        SteerStarted,
        ThreadCarry,
        ThreadChanged,
        ThreadInput,
        ThreadProgress,
        ThreadResult,
        request_workflow_id,
        update_failure,
    )

TERMINAL: frozenset[ThreadState] = frozenset(("closed", "expired"))
SIDES: tuple[Side, Side] = ("requester", "responder")
# Consecutive turns that sent nothing, consulted no one and didn't close, after which
# the thread expires: with bare text kept private and no early close, two quiet sides
# would otherwise use up every turn (3.13 E3).
SILENT_TURNS_TO_EXPIRE = 2
# How long an Edit waits for the item it names to be tracked, when an activity still
# running may be opening it. An id this run never tracks isn't waited on for longer.
STEER_OPENING_WAIT = timedelta(seconds=30)


@dataclass(slots=True)
class _Open:
    """An escalation this thread waits on, with what its validators need."""

    escalation_id: UUID
    kind: EscalationKind
    side: Side
    org_id: UUID
    consult_id: UUID | None = None
    consult_turn: int = 1
    # The draft revs written so far, so a resolution names one the member could see.
    revs: set[int] = field(default_factory=set[int])
    # The last rev handed out, written or still being drafted.
    last_rev: int = 1
    # A steer's rewrite is writing `last_rev`.
    redrafting: bool = False
    resolving: bool = False
    redraft_task: asyncio.Task[SteerFailure | None] | None = None

    @property
    def newest_rev(self) -> int | None:
        return max(self.revs, default=None)

    def add_rev(self, rev: int | None) -> None:
        """Record a written draft rev (a failed draft writes none)."""
        if rev is not None:
            self.revs.add(rev)

    def drafting(self, rev: int | None) -> bool:
        """Whether `rev` is the one a running rewrite is writing."""
        return self.redrafting and rev == self.last_rev and rev not in self.revs

    def stop_redraft(self) -> None:
        """Stop a rewrite's model call. A cancelled activity reaches `_rewrite` as an
        `ActivityError`, like a failed one."""
        if self.redraft_task is not None:
            self.redraft_task.cancel()


@dataclass(frozen=True, slots=True)
class _Steering:
    """The steer running for a side, and who sent it (for `STEER_RUNNING_ERROR`)."""

    steer_id: UUID
    member_id: UUID


def other(side: Side) -> Side:
    return "responder" if side == "requester" else "requester"


def _part_failure(part: asyncio.Task[SteerFailure | None]) -> SteerFailure | None:
    """Why a steer's part didn't land; cancelled before it started, by the run's end."""
    return "thread_closed" if part.cancelled() else part.result()


def _cut_off(tool_use_id: str | None, text: str, *, is_error: bool) -> OwedResult | None:
    """The result owed to a call whose person never decided before the thread ended."""
    if not tool_use_id:
        return None
    return OwedResult(tool_use_id=tool_use_id, content=text, is_error=is_error)


def _conflict(failed: ActivityError) -> bool:
    """`record_steer` found the id stored with another body."""
    cause = failed.cause
    return isinstance(cause, ApplicationError) and cause.type == STEER_CONFLICT_ERROR


@workflow.defn
class ThreadWorkflow:
    @workflow.init
    def __init__(self, thread: ThreadInput) -> None:
        self._input = thread
        carry = thread.carry
        # A reopened run starts as the ended thread it reopens, until `begin_reopen`:
        # Edit and resolutions are refused meanwhile, as on any ended thread.
        self._state: ThreadState = thread.reopen.from_state if thread.reopen else "active"
        self._side_to_move: Side | None = None if thread.reopen else "requester"
        self._turns: dict[Side, int] = {s: carry.turns.get(s, 0) if carry else 0 for s in SIDES}
        self._owed: dict[Side, list[OwedResult]] = {
            s: list(carry.owed.get(s, [])) if carry else [] for s in SIDES
        }
        self._open_escalations: dict[UUID, _Open] = {}
        self._resolved: dict[UUID, ResolutionApplied] = {}
        self._delivered: dict[Side, int] = {
            s: carry.delivered.get(s, 0) if carry else 0 for s in SIDES
        }
        self._consults: dict[Side, int] = {
            s: carry.consults.get(s, 0) if carry else 0 for s in SIDES
        }
        self._closed_by: Side | None = None
        self._last_stop: TurnStop | None = None
        self._silent_turns = 0
        # Whether a message has reached the responder: until one has, it has nothing to
        # answer and is never woken.
        self._crossed = False
        # Insertion-ordered (a set iterates by object address): the end of the run
        # cancels these in order, and a replay must issue the same cancel commands.
        self._tasks: dict[asyncio.Task[Any], None] = {}
        # Steers running beside the thread. Never cancelled: each settles its steer from
        # however its parts (in `_tasks`) ended, and the run waits for that.
        self._steers: dict[asyncio.Task[None], None] = {}
        # Activities running that may open an item on this thread (a turn that holds a
        # message, an answer that escalates). The item is in the database, and so in
        # the inbox, from their commit; Updates for it wait until it is tracked.
        self._opening = 0
        # The steer running for each side; the side's next turn waits for it.
        self._steering: dict[Side, _Steering | None] = {"requester": None, "responder": None}
        # The newest guidance version each side's steers wrote, for its next turn.
        self._guidance: dict[Side, int | None] = {s: thread.guidance.get(s) for s in SIDES}
        # The newest steer `seq` this run knows per side: the turn guard's watermark.
        self._steer_seq: dict[Side, int] = {s: thread.steer_seq.get(s, 0) for s in SIDES}
        # The thread's highest message seq when this run began (the close guard).
        self._run_start_seq = 0
        # The side that reopened this run, until its first turn, which is told so.
        self._reopening: Side | None = None
        # Items this run settled or cancelled: an Edit naming one doesn't wait for it.
        self._settled: set[UUID] = set()

    @workflow.run
    async def run(self, thread: ThreadInput) -> ThreadResult:
        if thread.reopen is not None and not await self._begin_reopen(thread.reopen):
            # Nothing changed: the thread is still ended, as it was.
            await workflow.wait_condition(workflow.all_handlers_finished)
            return ThreadResult(state=self._state, turns=self._total_turns())
        side: Side = thread.reopen.side if thread.reopen else "requester"
        while self._state not in TERMINAL:
            if self._total_turns() >= thread.max_turns:
                await self._end("expired", end_reason="turn_limit")
                break
            if self._past_deadline():
                await self._end("expired", end_reason="deadline")
                break
            await self._move(side)
            if not await self._steer_landed(side):
                continue  # The deadline passed: the check above ends the thread.
            steer_seq = self._steer_seq[side]
            result = await self._rep_turn(side)
            if not isinstance(result, RepTurnResult):
                await self._end("closed", end_reason="stopped", stop_reason=result)
                break
            side = await self._after_turn(side, result, steered=self._steer_seq[side] > steer_seq)
        if self._closed_by is not None and not self._past_deadline():
            # Its effects don't matter to the thread: only its summary can be written.
            last = other(self._closed_by)
            result = await self._rep_turn(last)
            if not isinstance(result, RepTurnResult):
                await self._note_unsummarized(last, result)
            else:
                # Its `close` is owed `CLOSED`, should the thread be reopened.
                self._owe(last, result.owed)
                if result.stop == "refusal":
                    await self._note_unsummarized(last, "refused")
        # A rewrite nobody can use any more must not keep calling the model, nor a
        # read-back wait on an item the ended thread cancelled.
        for task in self._tasks:
            task.cancel()
        # A steer cut short settles as `thread_closed`, or `done` if its parts landed.
        await workflow.wait_condition(
            lambda: not self._tasks and not self._steers and workflow.all_handlers_finished()
        )
        if thread.run == 1:
            await self._signal_request()
        else:
            await self._end_round()
        await self._finish_run()
        return ThreadResult(state=self._state, turns=self._total_turns())

    # --- Updates and query --------------------------------------------------------------

    @workflow.update
    async def resolve_escalation(self, resolution: EscalationResolution) -> EscalationResolved:
        """Apply a member's resolution (`apply_resolution`) and resume the thread. On a
        consult, `approve` releases the answer (the member's `text`, else the draft) as
        the representative's owed `consult` result, and `decline` owes it a refusal. On
        an `outbound_review`, `approve` sends the held message (`text` replaces it) and
        `decline` drops it. `take_over` sends `text` as the member's own message. A
        resolution is pinned to a draft rev (`draft_rev`, else the newest rev written),
        so a steer's rewrite landing meanwhile never changes what is sent or released. An
        item (or rev) still being written is resolved once it is. When the apply fails,
        the item is held until the database is read back: resolved there (the apply
        failed after its commit), the thread moves on; still open, it can be resolved
        again."""
        await self._ready(resolution.escalation_id, resolution.draft_rev)
        item, resolution = self._check_resolution(resolution)
        item.resolving = True
        arg = ApplyResolutionInput(
            world_id=self._input.world_id,
            thread_id=self._input.thread_id,
            resolution=resolution,
            message_id=workflow.uuid4(),
        )
        try:
            applied = await workflow.execute_activity_method(
                NetworkActivities.apply_resolution, arg, **main_activity_options()
            )
        except ActivityError as failed:
            # It may have failed after its commit: the item stays held until the
            # database says whether it was resolved (`_read_back`).
            self._spawn(self._read_back(item, arg))
            raise update_failure(failed) from failed
        item.resolving = False
        self._settle(item, applied)
        return applied.resolved

    @resolve_escalation.validator
    def validate_resolve_escalation(self, resolution: EscalationResolution) -> None:
        """Reject with `ESCALATION_NOT_OPEN_ERROR` unless the escalation is open on this
        thread; with `NOT_IN_ORG_ERROR` unless `resolution.org_id` is the escalation's
        org; with `INVALID_RESOLUTION_ERROR` for `take_over` without `text`, a consult
        `approve` with neither `text` nor a written draft, or a `draft_rev` not yet
        written. Accepts an item or rev still being written (`_pending`)."""
        if not self._pending(resolution.escalation_id):
            self._check_resolution(resolution, ready=False)

    @workflow.update
    async def reassign(self, reassignment: Reassignment) -> Reassigned:
        await self._ready(reassignment.escalation_id)
        self._open_item(reassignment.escalation_id)
        try:
            return await workflow.execute_activity_method(
                NetworkActivities.reassign_escalation,
                ReassignInput(world_id=self._input.world_id, reassignment=reassignment),
                **main_activity_options(),
            )
        except ActivityError as failed:
            raise update_failure(failed) from failed

    @reassign.validator
    def validate_reassign(self, reassignment: Reassignment) -> None:
        """Reject with `ESCALATION_NOT_OPEN_ERROR` unless the escalation is open on this
        thread, or may be about to be (`_pending`)."""
        if not self._pending(reassignment.escalation_id):
            self._open_item(reassignment.escalation_id)

    @workflow.update
    async def steer(self, steer: SteerRequest) -> SteerStarted:
        """A member's Edit (docs/design/EDIT.md §3.1): record it, then write the side's
        guidance and, when the member was looking at one of the side's waiting items,
        rewrite its draft as the next rev. Returns once the steer is recorded and the
        rev reserved, not when either part lands: the `steer` snapshot reports that. An
        item still being opened is waited for, briefly (`STEER_OPENING_WAIT`); one that
        can't be rewritten now leaves the Edit as guidance only."""
        target = steer.escalation_id
        if target is not None and target not in self._settled and self._pending(target):
            with contextlib.suppress(TimeoutError):
                await workflow.wait_condition(
                    lambda: not self._pending(target), timeout=STEER_OPENING_WAIT
                )
        self._check_steer(steer)
        side = steer.side
        self._steering[side] = _Steering(steer.steer_id, steer.member_id)
        item = self._steer_target(steer)
        rev = self._reserve(item)
        try:
            recorded = await workflow.execute_activity_method(
                NetworkActivities.record_steer,
                RecordSteerInput(
                    world_id=self._input.world_id,
                    thread_id=self._input.thread_id,
                    steer=steer,
                    draft_rev=rev,
                ),
                **main_activity_options(),
            )
        except ActivityError as failed:
            self._unreserve(item)
            self._steering[side] = None
            if not _conflict(failed):
                # It may have failed after its commit: a row left `working` would never
                # settle. (A conflict's id is another submission's, which settles itself.)
                self._spawn_steer(self._settle_unrecorded(steer, failure_reason(failed)))
            raise update_failure(failed) from failed
        self._steer_seq[side] = max(self._steer_seq[side], recorded.seq)
        if self._state in TERMINAL:
            # It ended while the steer was recorded: nothing is started now.
            self._unreserve(item)
            await self._settle_steer(steer.steer_id, failure="thread_closed")
            self._steering[side] = None
            return SteerStarted(steer_id=steer.steer_id, escalation_id=None, draft_rev=None)
        if item is not None and (
            item.escalation_id not in self._open_escalations or item.resolving
        ):
            # Resolved while the steer was recorded: guidance only.
            self._unreserve(item)
            item = rev = None
        self._spawn_steer(self._run_steer(steer, recorded, item, rev))
        return SteerStarted(
            steer_id=steer.steer_id,
            escalation_id=item.escalation_id if item is not None else None,
            draft_rev=rev,
        )

    @steer.validator
    def validate_steer(self, steer: SteerRequest) -> None:
        """Reject with `THREAD_CLOSED_ERROR` once the thread has ended, `NOT_IN_ORG_ERROR`
        unless `org_id` is the side's org, and `STEER_RUNNING_ERROR` (with who sent it)
        while a steer runs for that side."""
        self._check_steer(steer)

    @workflow.query
    def state(self) -> ThreadProgress:
        return ThreadProgress(
            thread_id=self._input.thread_id,
            state=self._state,
            side_to_move=self._side_to_move,
            requester_turns=self._turns["requester"],
            responder_turns=self._turns["responder"],
            open_escalation_ids=sorted(self._open_escalations),
            owed={side: list(results) for side, results in self._owed.items()},
            last_stop=self._last_stop,
        )

    # --- Validation ---------------------------------------------------------------------

    def _pending(self, escalation_id: UUID, draft_rev: int | None = None) -> bool:
        """Whether an activity still running may yet open this item on the thread, or
        write this rev of it: the database shows both from its commit, before this
        workflow tracks them."""
        item = self._open_escalations.get(escalation_id)
        if item is None:
            return self._opening > 0
        return item.drafting(draft_rev)

    async def _ready(self, escalation_id: UUID, draft_rev: int | None = None) -> None:
        await workflow.wait_condition(lambda: not self._pending(escalation_id, draft_rev))

    def _open_item(self, escalation_id: UUID) -> _Open:
        item = self._open_escalations.get(escalation_id)
        if item is None or item.resolving:
            raise ApplicationError(
                f"escalation {escalation_id} is not open on this thread",
                type=ESCALATION_NOT_OPEN_ERROR,
            )
        return item

    def _check_resolution(
        self, resolution: EscalationResolution, *, ready: bool = True
    ) -> tuple[_Open, EscalationResolution]:
        """The open item and the resolution as it will be applied (pinned to a rev).
        Unless `ready`, the rev a running rewrite is writing passes too."""
        item = self._open_item(resolution.escalation_id)
        if resolution.org_id != item.org_id:
            raise ApplicationError(
                "the member isn't in the organization this escalation belongs to",
                type=NOT_IN_ORG_ERROR,
            )
        text = (resolution.text or "").strip()
        if resolution.action == "take_over" and not text:
            raise ApplicationError("take_over needs text", type=INVALID_RESOLUTION_ERROR)
        rev = resolution.draft_rev if resolution.draft_rev is not None else item.newest_rev
        if rev is not None and rev not in item.revs and (ready or not item.drafting(rev)):
            raise ApplicationError(
                f"draft rev {rev} isn't ready; resolve a rev you can see",
                type=INVALID_RESOLUTION_ERROR,
            )
        if resolution.action == "approve" and not text and rev is None:
            raise ApplicationError(
                "there is no draft to approve; write the answer", type=INVALID_RESOLUTION_ERROR
            )
        if rev is not None and resolution.draft_rev != rev:
            resolution = resolution.model_copy(update={"draft_rev": rev})
        return item, resolution

    def _check_steer(self, steer: SteerRequest) -> None:
        if self._state in TERMINAL:
            raise ApplicationError("this conversation has ended", type=THREAD_CLOSED_ERROR)
        if steer.org_id != self._org(steer.side):
            raise ApplicationError(
                "the member isn't in the organization on that side", type=NOT_IN_ORG_ERROR
            )
        running = self._steering[steer.side]
        if running is not None:
            raise ApplicationError(
                "Claude is still working on an earlier edit",
                {"by_member_id": str(running.member_id)},
                type=STEER_RUNNING_ERROR,
            )

    def _steer_target(self, steer: SteerRequest) -> _Open | None:
        """The steer's item, when it can be rewritten now: tracked, open, the steer's
        side's, not being resolved and not already being rewritten."""
        if steer.escalation_id is None:
            return None
        item = self._open_escalations.get(steer.escalation_id)
        if item is None or item.resolving or item.redrafting or item.side != steer.side:
            return None
        if item.kind == "consult" and item.consult_id is None:
            return None
        return item

    def _reserve(self, item: _Open | None) -> int | None:
        """Reserve the item's next rev for a rewrite."""
        if item is None:
            return None
        item.redrafting = True
        item.last_rev += 1
        return item.last_rev

    def _unreserve(self, item: _Open | None) -> None:
        """Give the reservation up; the rev number stays skipped."""
        if item is not None:
            item.redrafting = False

    # --- Turns ----------------------------------------------------------------------------

    async def _rep_turn(self, side: Side) -> RepTurnResult | FailureReason:
        """One turn of `side`, opening with what it is owed; why it failed, if it did."""
        thread = self._input
        org_id = self._org(side)
        self._turns[side] += 1
        turn = self._turns[side]
        owed, self._owed[side] = self._owed[side], []
        self._opening += 1
        try:
            result = await workflow.execute_activity_method(
                AgentActivities.rep_turn,
                RepTurnInput(
                    world_id=thread.world_id,
                    thread_id=thread.thread_id,
                    side=side,
                    org_id=org_id,
                    cassette_thread=thread.cassette_thread,
                    turn_id=turn_id_for(thread.world_id, thread.thread_id, org_id, turn),
                    turn=turn,
                    owed=owed,
                    deliver_after_seq=self._delivered[side],
                    guidance_version=self._guidance[side],
                    steer_seq=self._steer_seq[side],
                    reopened=self._reopening == side,
                    run_start_seq=self._run_start_seq,
                ),
                **model_activity_options(),
            )
        except ActivityError as failed:
            workflow.logger.warning("%s turn %d failed: %s", side, turn, failed)
            return failure_reason(failed)
        finally:
            # A held message is tracked (`_held`) before the workflow next yields.
            self._opening -= 1
            if self._reopening == side:
                self._reopening = None
        self._delivered[side] = result.delivered_through_seq
        self._last_stop = result.stop
        return result

    async def _note_unsummarized(self, side: Side, reason: FailureReason) -> None:
        """`side`'s last turn failed or was refused (`reason`): tell its org why it has
        no summary."""
        thread = self._input
        org_id = self._org(side)
        await workflow.execute_activity_method(
            NetworkActivities.note_unsummarized,
            UnsummarizedInput(
                world_id=thread.world_id,
                thread_id=thread.thread_id,
                org_id=org_id,
                turn_id=turn_id_for(thread.world_id, thread.thread_id, org_id, self._turns[side]),
                reason=reason,
            ),
            **mirror_activity_options(),
        )

    async def _after_turn(self, side: Side, result: RepTurnResult, *, steered: bool) -> Side:
        """Apply how the turn ended; returns the side that moves next. `steered`: a steer
        of the side's was recorded while the turn ran."""
        effects = result.effects
        quiet = result.stop != "refusal" and not any(
            isinstance(e, MessageSent | ConsultOpened | MessageHeld) for e in effects
        )
        if quiet and steered:
            # The Edit refused its close (`STEERED_NOT_CLOSED`), or came after it chose
            # to wait: nothing went out, so the move is still its own, and its next turn
            # opens with the new guidance. Not a silent turn the thread expires on.
            self._owe(side, result.owed)
            return side
        self._silent_turns = self._silent_turns + 1 if quiet else 0
        if any(isinstance(e, MessageSent) and e.message_kind == "message" for e in effects):
            # The row handed the move over with the first message (`append_message`).
            self._side_to_move = other(side)
            self._crossed = True
        if any(isinstance(e, MessageSent) and e.message_kind == "close" for e in effects):
            # `CLOSED`, owed should the thread be reopened.
            self._owe(side, result.owed)
            await self._close(side)
            return other(side)
        consult = next((e for e in effects if isinstance(e, ConsultOpened)), None)
        if consult is not None:
            return await self._consult(side, consult, result.owed)
        held = next((e for e in effects if isinstance(e, MessageHeld)), None)
        if held is not None:
            return await self._held(side, held, result.owed)
        if result.stop == "refusal":
            if self._crossed:
                await self._close(side, stop_reason="refused")
            else:
                # Nothing reached the responder: it has nothing to summarize, and is
                # never woken on a thread nobody wrote to (3.13 E3). Who refused is
                # still stored: only they may reopen it.
                await self._end(
                    "closed", end_reason="stopped", stop_reason="refused", closed_by=side
                )
            return other(side)
        self._owe(side, result.owed)
        if not self._crossed:
            await self._end("expired", end_reason="unsent")
        elif self._silent_turns >= SILENT_TURNS_TO_EXPIRE:
            await self._end("expired", end_reason="quiet")
        return other(side)

    def _owe(self, side: Side, owed: OwedResult | None, *, tool_use_id: str | None = None) -> None:
        """Owe `side`'s next turn a known result, answering the call `tool_use_id` (the
        call that ended its turn) when the result came from elsewhere."""
        if owed is None or owed.content is None:
            return
        if tool_use_id:
            owed = owed.model_copy(update={"tool_use_id": tool_use_id})
        self._owed[side].append(owed)

    async def _consult(self, side: Side, opened: ConsultOpened, owed: OwedResult | None) -> Side:
        """The blocking consult: released → the same side again with the answer;
        escalated → wait for a person."""
        thread = self._input
        tool_use_id = owed.tool_use_id if owed is not None else None
        await self._set_state("awaiting_principal", side)
        if not opened.deduplicated:
            self._consults[side] += 1
        self._opening += 1
        try:
            decision = await workflow.execute_activity_method(
                AgentActivities.answer_consult,
                AnswerConsultInput(
                    world_id=thread.world_id,
                    consult_id=opened.consult_id,
                    thread_id=thread.thread_id,
                    side=side,
                    org_id=self._org(side),
                    cassette_thread=thread.cassette_thread,
                    consult_turn=max(self._consults[side], 1),
                ),
                **model_activity_options(),
            )
        except ActivityError as failed:
            workflow.logger.warning("consult %s failed: %s", opened.consult_id, failed)
            await self._end("closed", end_reason="stopped", stop_reason=failure_reason(failed))
            return side
        finally:
            self._opening -= 1
        if not isinstance(decision, ConsultEscalated):
            self._owe(side, decision.owed, tool_use_id=tool_use_id)
            return side
        item = self._track(decision.escalation_id, "consult", side, decision)
        applied = await self._wait_resolution(item.escalation_id)
        if applied is None:
            self._owe(side, _cut_off(tool_use_id, CUT_OFF_CONSULT, is_error=False))
            return side
        self._owe(side, applied.owed, tool_use_id=tool_use_id)
        # `take_over`: the member wrote to the counterparty, who answers next.
        return self._sent_on(side) if applied.sent else side

    async def _held(self, side: Side, held: MessageHeld, owed: OwedResult | None) -> Side:
        """*Ask*: the representative's message waits for a person. The call's result is
        owed then, and the thread moves on as after whatever reached the counterparty:
        the approved message, the member's own (`take_over`), or nothing, and the same
        side moves again."""
        tool_use_id = owed.tool_use_id if owed is not None else None
        # Tracked before anything is awaited: the item is open since the turn's commit.
        item = self._track(held.escalation_id, "outbound_review", side)
        # The tool already holds the row `awaiting_principal`.
        self._state, self._side_to_move = "awaiting_principal", side
        await self._signal_request()
        applied = await self._wait_resolution(item.escalation_id)
        if applied is None:
            self._owe(side, _cut_off(tool_use_id, CUT_OFF_HELD, is_error=True))
            return side
        self._owe(side, applied.owed, tool_use_id=tool_use_id)
        return self._sent_on(side) if applied.sent else side

    def _sent_on(self, side: Side) -> Side:
        """A person let a message of `side`'s through: the other side answers next."""
        self._crossed = True
        return other(side)

    def _track(
        self,
        escalation_id: UUID,
        kind: EscalationKind,
        side: Side,
        escalated: ConsultEscalated | None = None,
    ) -> _Open:
        item = _Open(
            escalation_id=escalation_id,
            kind=kind,
            side=side,
            org_id=self._org(side),
            consult_id=escalated.consult_id if escalated else None,
            consult_turn=max(self._consults[side], 1),
            last_rev=(escalated.draft_rev or 1) if escalated else 1,
        )
        if escalated is not None:
            item.add_rev(escalated.draft_rev)
        elif kind == "outbound_review":
            # The held message is rev 1 (`RepToolExecutor._hold`).
            item.add_rev(1)
        self._open_escalations[escalation_id] = item
        return item

    def _settle(self, item: _Open, applied: ResolutionApplied) -> None:
        """The item is resolved: the thread moves on with what the resolution did."""
        self._open_escalations.pop(item.escalation_id, None)
        self._settled.add(item.escalation_id)
        item.stop_redraft()
        self._resolved[item.escalation_id] = applied

    async def _read_back(self, item: _Open, arg: ApplyResolutionInput) -> None:
        """After a failed apply: what the database holds for the item. Resolved, the
        thread moves on as if the apply had returned; still open, a member may resolve
        it again."""
        try:
            stored = await workflow.execute_activity_method(
                NetworkActivities.stored_resolution, arg, **mirror_activity_options()
            )
        finally:
            item.resolving = False
        if stored is not None:
            self._settle(item, stored)

    def _spawn[T](self, work: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """Run `work` beside the thread; the run cancels what is left when it ends."""
        task = asyncio.create_task(work)
        self._tasks[task] = None
        task.add_done_callback(lambda done: self._tasks.pop(done, None))
        return task

    def _spawn_steer(self, work: Coroutine[Any, Any, None]) -> None:
        """Run a steer's work beside the thread; the run waits for it when it ends."""
        task = asyncio.create_task(work)
        self._steers[task] = None
        task.add_done_callback(lambda done: self._steers.pop(done, None))

    async def _wait_resolution(self, escalation_id: UUID) -> ResolutionApplied | None:
        """Wait for a member, bounded only by `deadline_at`; None (and the thread
        `expired`) when the deadline passed first."""
        if not await self._wait(lambda: escalation_id in self._resolved):
            await self._end("expired", end_reason="deadline")
            return None
        return self._resolved.pop(escalation_id)

    # --- Steers -------------------------------------------------------------------------

    async def _run_steer(
        self, steer: SteerRequest, recorded: SteerRecorded, item: _Open | None, rev: int | None
    ) -> None:
        """The steer's guidance and its item's rewrite, side by side, then its outcome
        (EDIT.md §3.2). Each part fails on its own; the run's end cancels them, never
        the settle."""
        try:
            parts = [self._spawn(self._guidance_part(steer))]
            if item is not None and rev is not None:
                item.redraft_task = self._spawn(
                    self._rewrite(item, rev, steer, recorded.author_name)
                )
                parts.append(item.redraft_task)
            await workflow.wait(parts)
            failure: SteerFailure | None = None
            for part in parts:
                failure = failure or _part_failure(part)
            if self._state in TERMINAL and failure is None:
                # A rewrite the thread's end stopped: the item was cancelled with it.
                failure = "thread_closed"
            await self._settle_steer(steer.steer_id, failure=failure)
        finally:
            self._steering[steer.side] = None

    async def _guidance_part(self, steer: SteerRequest) -> SteerFailure | None:
        """Write the side's guidance; why the call failed, if it did. A rejected
        guidance is an outcome the activity stores, not a failure here."""
        try:
            written = await workflow.execute_activity_method(
                AgentActivities.write_guidance,
                SteerPartInput(
                    world_id=self._input.world_id,
                    steer_id=steer.steer_id,
                    cassette_thread=self._input.cassette_thread,
                ),
                **model_activity_options(),
            )
        except ActivityError as failed:
            if isinstance(failed.cause, CancelledError):
                return "thread_closed"  # Only the run's end cancels it.
            workflow.logger.warning("steer %s guidance failed: %s", steer.steer_id, failed)
            return failure_reason(failed)
        if written.outcome == "updated" and written.version is not None:
            self._guidance[steer.side] = written.version
        return None

    async def _rewrite(
        self, item: _Open, rev: int, steer: SteerRequest, author_name: str
    ) -> SteerFailure | None:
        """Write rev `rev` of the item as the steer asks: the answerer's redraft for a
        consult, one rewrite call for a held message. Stopped (not failed) when the item
        is resolved or the thread ends."""
        thread = self._input
        try:
            if item.kind == "consult" and item.consult_id is not None:
                decision = await workflow.execute_activity_method(
                    AgentActivities.answer_consult,
                    AnswerConsultInput(
                        world_id=thread.world_id,
                        consult_id=item.consult_id,
                        thread_id=thread.thread_id,
                        side=item.side,
                        org_id=item.org_id,
                        cassette_thread=thread.cassette_thread,
                        consult_turn=item.consult_turn,
                        draft_rev=rev,
                        instruction=steer.body,
                        base_text=steer.draft_text,
                        member_name=author_name,
                    ),
                    **model_activity_options(),
                )
                if isinstance(decision, ConsultEscalated):
                    item.add_rev(decision.draft_rev)
            else:
                item.add_rev(
                    await workflow.execute_activity_method(
                        AgentActivities.rewrite_held,
                        SteerPartInput(
                            world_id=thread.world_id,
                            steer_id=steer.steer_id,
                            cassette_thread=thread.cassette_thread,
                        ),
                        **model_activity_options(),
                    )
                )
        except ActivityError as failed:
            if self._stopped(item):
                return None
            workflow.logger.warning("rewrite %d of %s failed: %s", rev, item.escalation_id, failed)
            return failure_reason(failed)
        finally:
            item.redrafting = False
            item.redraft_task = None
        return None

    def _stopped(self, item: _Open) -> bool:
        """Whether the item was resolved or its thread ended: a rewrite of it was then
        stopped (`stop_redraft`), not failed."""
        return item.escalation_id not in self._open_escalations

    async def _settle_steer(self, steer_id: UUID, *, failure: SteerFailure | None) -> int | None:
        """`settle_steer`: the steer's outcome, from what its parts wrote. Its `seq`, or
        None when it was never stored or the settle failed."""
        try:
            return await workflow.execute_activity_method(
                NetworkActivities.settle_steer,
                SettleSteerInput(world_id=self._input.world_id, steer_id=steer_id, failure=failure),
                **mirror_activity_options(),
            )
        except ActivityError as failed:
            workflow.logger.warning("couldn't settle steer %s: %s", steer_id, failed)
            return None

    async def _settle_unrecorded(self, steer: SteerRequest, failure: SteerFailure) -> None:
        """Settle a steer whose `record_steer` failed. Stored all the same (it failed
        after its commit), its `seq` is the side's watermark from now on: the side's
        later turns would otherwise take it for an Edit arriving mid-turn, and hold or
        refuse their every send."""
        seq = await self._settle_steer(steer.steer_id, failure=failure)
        if seq is not None:
            self._steer_seq[steer.side] = max(self._steer_seq[steer.side], seq)

    # --- State ----------------------------------------------------------------------------

    async def _move(self, side: Side) -> None:
        """Hand the move to `side`, on the row too: the tools check whose turn it is."""
        if self._state != "active" or self._side_to_move != side:
            await self._set_state("active", side)

    async def _close(self, closer: Side, *, stop_reason: FailureReason | None = None) -> None:
        """`closer` ended the conversation (a `close`, or a refusal: `stop_reason`); the
        other side's last turn follows."""
        self._closed_by = closer
        await self._set_state(
            "closed",
            None,
            end_reason="stopped" if stop_reason else "closed",
            closed_by=closer,
            last_turn=True,
            stop_reason=stop_reason,
        )

    async def _end(
        self,
        state: ThreadState,
        *,
        end_reason: EndReason,
        stop_reason: FailureReason | None = None,
        closed_by: Side | None = None,
    ) -> None:
        if self._state not in TERMINAL:
            await self._set_state(
                state, None, end_reason=end_reason, closed_by=closed_by, stop_reason=stop_reason
            )

    async def _set_state(
        self,
        state: ThreadState,
        side_to_move: Side | None,
        *,
        end_reason: EndReason | None = None,
        closed_by: Side | None = None,
        last_turn: bool = False,
        stop_reason: FailureReason | None = None,
    ) -> None:
        changed = state != self._state
        self._state = state
        self._side_to_move = side_to_move
        if state in TERMINAL:
            # `set_thread_state` cancels them: nobody can act on an ended thread.
            for item in self._open_escalations.values():
                item.stop_redraft()
            self._settled.update(self._open_escalations)
            self._open_escalations.clear()
        await workflow.execute_activity_method(
            NetworkActivities.set_thread_state,
            ThreadStateInput(
                world_id=self._input.world_id,
                thread_id=self._input.thread_id,
                state=state,
                side_to_move=side_to_move,
                end_reason=end_reason,
                closed_by=closed_by,
                last_turn=last_turn,
                stop_reason=stop_reason,
            ),
            **mirror_activity_options(),
        )
        # The terminal state reaches the request once, at the end of the run.
        if changed and state not in TERMINAL:
            await self._signal_request()

    async def _signal_request(self) -> None:
        """Tell the request workflow the state moved. It follows only first runs: a
        reopened run closes the request itself (`_end_round`)."""
        thread = self._input
        if thread.run > 1:
            return
        try:
            await workflow.get_external_workflow_handle(
                request_workflow_id(thread.world_id, thread.request_id)
            ).signal(
                "thread_changed",
                ThreadChanged(
                    thread_id=thread.thread_id,
                    responder_org_id=thread.responder_org_id,
                    state=self._state,
                ),
            )
        except FailureError as failed:
            # The request may have closed already (a seeded one).
            workflow.logger.info("request %s didn't take the signal: %s", thread.request_id, failed)

    # --- Runs -----------------------------------------------------------------------------

    async def _begin_reopen(self, reopen: ReopenInput) -> bool:
        """The private part of a reopen, then the reopen itself (REOPEN.md §4.3): the
        steer recorded, the side's guidance written from it, and only when that changed,
        `begin_reopen`. False, with the steer settled failed and nothing else written,
        when any step didn't land."""
        thread = self._input
        side = reopen.side
        try:
            seq = await workflow.execute_activity_method(
                NetworkActivities.record_reopen,
                RecordReopenInput(
                    world_id=thread.world_id, thread_id=thread.thread_id, reopen=reopen
                ),
                **mirror_activity_options(),
            )
        except ActivityError as failed:
            workflow.logger.warning("reopen %s wasn't recorded: %s", reopen.steer_id, failed)
            if not _conflict(failed):
                await self._settle_steer(reopen.steer_id, failure="reopen_failed")
            return False
        # The reopen's own steer isn't one that arrived mid-turn (EDIT.md §3.7).
        self._steer_seq[side] = max(self._steer_seq[side], seq)
        try:
            written = await workflow.execute_activity_method(
                AgentActivities.write_guidance,
                SteerPartInput(
                    world_id=thread.world_id,
                    steer_id=reopen.steer_id,
                    cassette_thread=thread.cassette_thread,
                ),
                **model_activity_options(),
            )
        except ActivityError as failed:
            workflow.logger.warning("reopen %s guidance failed: %s", reopen.steer_id, failed)
            await self._settle_steer(reopen.steer_id, failure=failure_reason(failed))
            return False
        if written.outcome != "updated" or written.version is None:
            # Unchanged or rejected: `settle_steer` reads why from the steer's row.
            await self._settle_steer(reopen.steer_id, failure=None)
            return False
        self._guidance[side] = written.version
        try:
            start_seq = await workflow.execute_activity_method(
                NetworkActivities.begin_reopen,
                BeginReopenInput(
                    world_id=thread.world_id,
                    thread_id=thread.thread_id,
                    run=thread.run,
                    steer_id=reopen.steer_id,
                    deadline_at=thread.deadline_at,
                    max_turns=thread.max_turns,
                ),
                **mirror_activity_options(),
            )
        except ActivityError as failed:
            workflow.logger.warning("reopen %s didn't begin: %s", reopen.steer_id, failed)
            await self._settle_steer(reopen.steer_id, failure="reopen_failed")
            return False
        self._state, self._side_to_move = "active", side
        self._run_start_seq = start_seq
        self._reopening = side
        return True

    async def _end_round(self) -> None:
        """A reopened run's end: close the request if nothing on it is live, and tell
        its chat which round ended."""
        thread = self._input
        try:
            ended = await workflow.execute_activity_method(
                NetworkActivities.end_round,
                EndRoundInput(
                    world_id=thread.world_id, request_id=thread.request_id, from_workflow=False
                ),
                **mirror_activity_options(),
            )
            if ended.closed and ended.conversation_id is not None:
                await workflow.execute_activity_method(
                    NetworkActivities.notify_conversation,
                    NotifyConversationInput(
                        world_id=thread.world_id,
                        conversation_id=ended.conversation_id,
                        update=NetworkUpdate(
                            request_id=thread.request_id, kind="closed", round=ended.round
                        ),
                    ),
                    **mirror_activity_options(),
                )
        except ActivityError as failed:
            workflow.logger.warning("request %s round didn't end: %s", thread.request_id, failed)

    async def _finish_run(self) -> None:
        """The run's last activity: how it ended and what the next run carries."""
        thread = self._input
        try:
            await workflow.execute_activity_method(
                NetworkActivities.finish_run,
                FinishRunInput(
                    world_id=thread.world_id,
                    thread_id=thread.thread_id,
                    run=thread.run,
                    carry=ThreadCarry(
                        turns=dict(self._turns),
                        delivered=dict(self._delivered),
                        consults=dict(self._consults),
                        owed={side: list(results) for side, results in self._owed.items()},
                    ),
                ),
                **mirror_activity_options(),
            )
        except ActivityError as failed:
            workflow.logger.warning("run %d didn't finish: %s", thread.run, failed)

    async def _steer_landed(self, side: Side) -> bool:
        """Wait for the side's running steer, so its turn opens with the guidance it
        wrote. False once the deadline passes."""
        if self._steering[side] is None:
            return True  # Without a wait: one with a timeout schedules a timer.
        return await self._wait(lambda: self._steering[side] is None)

    async def _wait(self, condition: Callable[[], bool]) -> bool:
        """Wait for `condition`, never past the deadline."""
        left = self._deadline_left()
        if left is not None and left <= timedelta(0):
            return condition()
        try:
            await workflow.wait_condition(condition, timeout=left)
        except TimeoutError:
            return False
        return True

    def _deadline_left(self) -> timedelta | None:
        deadline = self._input.deadline_at
        if deadline is None:
            return None
        return max(deadline - workflow.now(), timedelta(0))

    def _past_deadline(self) -> bool:
        left = self._deadline_left()
        return left is not None and left <= timedelta(0)

    def _total_turns(self) -> int:
        return sum(self._turns.values())

    def _org(self, side: Side) -> UUID:
        thread = self._input
        return thread.requester_org_id if side == "requester" else thread.responder_org_id


__all__ = ["ThreadWorkflow"]
