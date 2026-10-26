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

Otherwise the other side moves next. A refusal closes the thread as a `close` without a
summary would; a failed turn closes it with no last turn. The thread ends `expired` at
`max_turns` (both sides' turns together) or at `deadline_at`, which bounds every wait,
a person's included, so the thread writes its own terminal state inside the execution
timeout: these are §5's turn cap and idle timeout. Each state change is mirrored to the
row (`set_thread_state`, which the tools read to check whose turn it is) and signalled
to the request (`thread_changed`); the terminal state only once the last turn is done,
so the request never reports a thread before both summaries could be written.

Rep turn ids are `ids.turn_id_for(world, thread, org, side turn)`, as P0's turns are.

Owner: R-temporal.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, FailureError

with workflow.unsafe.imports_passed_through():
    from harmony.agents.models import EscalationResolution
    from harmony.agents.runtime import (
        ConsultOpened,
        MessageHeld,
        MessageSent,
        OwedResult,
        TurnStop,
    )
    from harmony.db.models.choices import EscalationKind, Side, ThreadState
    from harmony.ids import turn_id_for
    from harmony.temporal.activities.agents import (
        AgentActivities,
        AnswerConsultInput,
        ConsultEscalated,
        RepTurnInput,
        RepTurnResult,
    )
    from harmony.temporal.activities.network import (
        ApplyResolutionInput,
        NetworkActivities,
        ReassignInput,
        RedraftFailedInput,
        ResolutionApplied,
        ThreadStateInput,
    )
    from harmony.temporal.policies import (
        main_activity_options,
        mirror_activity_options,
        model_activity_options,
    )
    from harmony.temporal.workflows.payloads import (
        ESCALATION_NOT_OPEN_ERROR,
        INVALID_RESOLUTION_ERROR,
        NOT_IN_ORG_ERROR,
        EscalationResolved,
        Reassigned,
        Reassignment,
        Redraft,
        RedraftStarted,
        ThreadChanged,
        ThreadInput,
        ThreadProgress,
        ThreadResult,
        request_workflow_id,
        update_failure,
    )

TERMINAL: frozenset[ThreadState] = frozenset(("closed", "expired"))


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
    redrafting: bool = False
    resolving: bool = False
    redraft_task: asyncio.Task[None] | None = None

    @property
    def newest_rev(self) -> int | None:
        return max(self.revs, default=None)

    def add_rev(self, escalated: ConsultEscalated) -> None:
        """Record a written draft rev (a failed draft writes none)."""
        if escalated.draft_rev is not None:
            self.revs.add(escalated.draft_rev)

    def drafting(self, rev: int | None) -> bool:
        """Whether `rev` is the one a running redraft is writing."""
        return self.redrafting and rev == self.last_rev and rev not in self.revs

    def stop_redraft(self) -> None:
        """Stop a redraft's model call. A cancelled activity reaches `_redraft` as an
        `ActivityError`, like a failed one."""
        if self.redraft_task is not None:
            self.redraft_task.cancel()


def other(side: Side) -> Side:
    return "responder" if side == "requester" else "requester"


@workflow.defn
class ThreadWorkflow:
    @workflow.init
    def __init__(self, thread: ThreadInput) -> None:
        self._input = thread
        self._state: ThreadState = "active"
        self._side_to_move: Side | None = "requester"
        self._turns: dict[Side, int] = {"requester": 0, "responder": 0}
        self._owed: dict[Side, list[OwedResult]] = {"requester": [], "responder": []}
        self._open_escalations: dict[UUID, _Open] = {}
        self._resolved: dict[UUID, ResolutionApplied] = {}
        self._delivered: dict[Side, int] = {"requester": 0, "responder": 0}
        self._consults: dict[Side, int] = {"requester": 0, "responder": 0}
        self._closed_by: Side | None = None
        self._last_stop: TurnStop | None = None
        # Insertion-ordered (a set iterates by object address): the end of the run
        # cancels these in order, and a replay must issue the same cancel commands.
        self._tasks: dict[asyncio.Task[None], None] = {}
        # Activities running that may open an item on this thread (a turn that holds a
        # message, an answer that escalates). The item is in the database, and so in
        # the inbox, from their commit; Updates for it wait until it is tracked.
        self._opening = 0

    @workflow.run
    async def run(self, thread: ThreadInput) -> ThreadResult:
        side: Side = "requester"
        while self._state not in TERMINAL:
            if self._total_turns() >= thread.max_turns or self._past_deadline():
                await self._end("expired")
                break
            await self._move(side)
            result = await self._rep_turn(side)
            if result is None:
                await self._end("closed")
                break
            side = await self._after_turn(side, result)
        if self._closed_by is not None and not self._past_deadline():
            # Its effects don't matter to the thread: only its summary can be written.
            await self._rep_turn(other(self._closed_by))
        # A redraft nobody can use any more must not keep calling the model, nor a
        # read-back wait on an item the ended thread cancelled.
        for task in self._tasks:
            task.cancel()
        await workflow.wait_condition(lambda: not self._tasks and workflow.all_handlers_finished())
        await self._signal_request()
        return ThreadResult(state=self._state, turns=self._total_turns())

    # --- Updates and query --------------------------------------------------------------

    @workflow.update
    async def resolve_escalation(self, resolution: EscalationResolution) -> EscalationResolved:
        """Apply a member's resolution (`apply_resolution`) and resume the thread. On a
        consult, `approve` releases the answer (the member's `text`, else the draft) as
        the representative's owed `consult` result, and `decline` owes it a refusal. On
        an `outbound_review`, `approve` sends the held message (`text` replaces it) and
        `decline` drops it. `take_over` sends `text` as the member's own message. A
        consult resolution is pinned to a draft rev (`draft_rev`, else the newest rev
        written), so a redraft landing meanwhile never changes what is released. An item
        (or rev) still being written is resolved once it is. When the apply fails, the
        item is held until the database is read back: resolved there (the apply failed
        after its commit), the thread moves on; still open, it can be resolved again."""
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
    async def redraft(self, redraft: Redraft) -> RedraftStarted:
        """Run `answer_consult` for the next draft rev with the instruction. Returns
        once the rev is reserved, not when the draft is written; a rev that ends up
        unwritten is reported (`report_redraft_failed`). The redraft stops when the item
        is resolved or the thread ends."""
        await self._ready(redraft.escalation_id)
        item = self._redraftable(redraft)
        item.redrafting = True
        item.last_rev += 1
        rev = item.last_rev
        item.redraft_task = self._spawn(self._redraft(item, rev, redraft.instruction))
        return RedraftStarted(escalation_id=item.escalation_id, draft_rev=rev)

    @redraft.validator
    def validate_redraft(self, redraft: Redraft) -> None:
        """Reject with `ESCALATION_NOT_OPEN_ERROR` unless it is an open `consult`
        escalation on this thread with no redraft already running, or may be about to be
        one (`_pending`)."""
        if not self._pending(redraft.escalation_id):
            self._redraftable(redraft)

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
        Unless `ready`, the rev a running redraft is writing passes too."""
        item = self._open_item(resolution.escalation_id)
        if resolution.org_id != item.org_id:
            raise ApplicationError(
                "the member isn't in the organization this escalation belongs to",
                type=NOT_IN_ORG_ERROR,
            )
        text = (resolution.text or "").strip()
        if resolution.action == "take_over" and not text:
            raise ApplicationError("take_over needs text", type=INVALID_RESOLUTION_ERROR)
        if item.kind != "consult":
            return item, resolution
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

    def _redraftable(self, redraft: Redraft) -> _Open:
        item = self._open_item(redraft.escalation_id)
        if item.kind != "consult" or item.redrafting:
            raise ApplicationError(
                f"escalation {redraft.escalation_id} can't be redrafted now",
                type=ESCALATION_NOT_OPEN_ERROR,
            )
        return item

    # --- Turns ----------------------------------------------------------------------------

    async def _rep_turn(self, side: Side) -> RepTurnResult | None:
        """One turn of `side`, opening with what it is owed; None when it failed."""
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
                ),
                **model_activity_options(),
            )
        except ActivityError as failed:
            workflow.logger.warning("%s turn %d failed: %s", side, turn, failed)
            return None
        finally:
            # A held message is tracked (`_held`) before the workflow next yields.
            self._opening -= 1
        self._delivered[side] = result.delivered_through_seq
        self._last_stop = result.stop
        return result

    async def _after_turn(self, side: Side, result: RepTurnResult) -> Side:
        """Apply how the turn ended; returns the side that moves next."""
        effects = result.effects
        if any(isinstance(e, MessageSent) and e.message_kind == "message" for e in effects):
            # The row handed the move over with the first message (`append_message`).
            self._side_to_move = other(side)
        if any(isinstance(e, MessageSent) and e.message_kind == "close" for e in effects):
            await self._close(side)
            return other(side)
        consult = next((e for e in effects if isinstance(e, ConsultOpened)), None)
        if consult is not None:
            return await self._consult(side, consult, result.owed)
        held = next((e for e in effects if isinstance(e, MessageHeld)), None)
        if held is not None:
            return await self._held(side, held, result.owed)
        if result.stop == "refusal":
            await self._close(side)
            return other(side)
        self._owe(side, result.owed)
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
            await self._end("closed")
            return side
        finally:
            self._opening -= 1
        if not isinstance(decision, ConsultEscalated):
            self._owe(side, decision.owed, tool_use_id=tool_use_id)
            return side
        item = self._track(decision.escalation_id, "consult", side, decision)
        applied = await self._wait_resolution(item.escalation_id)
        if applied is None:
            return side
        self._owe(side, applied.owed, tool_use_id=tool_use_id)
        # `take_over`: the member wrote to the counterparty, who answers next.
        return other(side) if applied.sent else side

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
            return side
        self._owe(side, applied.owed, tool_use_id=tool_use_id)
        return other(side) if applied.sent else side

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
            item.add_rev(escalated)
        self._open_escalations[escalation_id] = item
        return item

    def _settle(self, item: _Open, applied: ResolutionApplied) -> None:
        """The item is resolved: the thread moves on with what the resolution did."""
        self._open_escalations.pop(item.escalation_id, None)
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

    def _spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Run `work` beside the thread; the run cancels what is left when it ends."""
        task = asyncio.create_task(work)
        self._tasks[task] = None
        task.add_done_callback(lambda done: self._tasks.pop(done, None))
        return task

    async def _wait_resolution(self, escalation_id: UUID) -> ResolutionApplied | None:
        """Wait for a member, bounded only by `deadline_at`; None (and the thread
        `expired`) when the deadline passed first."""
        if not await self._wait(lambda: escalation_id in self._resolved):
            await self._end("expired")
            return None
        return self._resolved.pop(escalation_id)

    async def _redraft(self, item: _Open, rev: int, instruction: str) -> None:
        thread = self._input
        try:
            if item.consult_id is None:
                return
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
                    instruction=instruction,
                ),
                **model_activity_options(),
            )
            if isinstance(decision, ConsultEscalated):
                item.add_rev(decision)
        except ActivityError as failed:
            if self._stopped(item):
                return
            workflow.logger.warning("redraft %d of %s failed: %s", rev, item.escalation_id, failed)
        finally:
            item.redrafting = False
            # Nothing left to stop: the report below must not be cancelled with it.
            item.redraft_task = None
        if rev not in item.revs and not self._stopped(item):
            await self._report_redraft_failed(item, rev)

    def _stopped(self, item: _Open) -> bool:
        """Whether the item was resolved or its thread ended: a redraft of it was then
        stopped (`stop_redraft`), not failed, and there is nobody to tell."""
        return item.escalation_id not in self._open_escalations

    async def _report_redraft_failed(self, item: _Open, rev: int) -> None:
        """A rev that was never written would leave the member waiting on it: say so.
        Best effort, as the panel also stops waiting on its own after a while."""
        try:
            await workflow.execute_activity_method(
                NetworkActivities.report_redraft_failed,
                RedraftFailedInput(
                    world_id=self._input.world_id, escalation_id=item.escalation_id, draft_rev=rev
                ),
                **main_activity_options(),
            )
        except ActivityError as failed:
            workflow.logger.warning(
                "couldn't report redraft %d of %s: %s", rev, item.escalation_id, failed
            )

    # --- State ----------------------------------------------------------------------------

    async def _move(self, side: Side) -> None:
        """Hand the move to `side`, on the row too: the tools check whose turn it is."""
        if self._state != "active" or self._side_to_move != side:
            await self._set_state("active", side)

    async def _close(self, closer: Side) -> None:
        """`closer` ended the conversation; the other side's last turn follows."""
        self._closed_by = closer
        await self._set_state("closed", None, closed_by=closer)

    async def _end(self, state: ThreadState) -> None:
        if self._state not in TERMINAL:
            await self._set_state(state, None)

    async def _set_state(
        self, state: ThreadState, side_to_move: Side | None, *, closed_by: Side | None = None
    ) -> None:
        changed = state != self._state
        self._state = state
        self._side_to_move = side_to_move
        if state in TERMINAL:
            # `set_thread_state` cancels them: nobody can act on an ended thread.
            for item in self._open_escalations.values():
                item.stop_redraft()
            self._open_escalations.clear()
        await workflow.execute_activity_method(
            NetworkActivities.set_thread_state,
            ThreadStateInput(
                world_id=self._input.world_id,
                thread_id=self._input.thread_id,
                state=state,
                side_to_move=side_to_move,
                closed_by=closed_by,
            ),
            **mirror_activity_options(),
        )
        # The terminal state reaches the request once, at the end of the run.
        if changed and state not in TERMINAL:
            await self._signal_request()

    async def _signal_request(self) -> None:
        thread = self._input
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
