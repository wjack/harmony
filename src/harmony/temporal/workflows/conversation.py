"""`ConversationWorkflow`: one internal Claude chat (§5, §12). ID `w:{world}:conv:{id}`.

The API never calls the model: it signal-with-starts this workflow (`member_message`,
`USE_EXISTING`) and the workflow runs internal turns on `harmony-chat`, one at a time:

1. wait until there's a queued member message or a `closed` network update (every
   thread of a request has ended), with no turn in flight and no card pending. A
   question the member's own representative asks them is no update: it shows in the
   chat from its card, answered there (the thread's `resolve_escalation`), and never
   needs a turn;
2. mint `turn_id` (`workflow.uuid4()`), bump the turn number, and run
   `AgentActivities.internal_turn` with everything queued, member messages first. A
   turn woken only by `closed` opens with the placeholder user turn plus a system note;
3. on `awaiting_card`, hold the turn until `resolve_card` (or a member message sent
   while the card was pending, which rejects it with that text as feedback) settles it,
   then run the same turn again from `next_step` with the `CardOutcome` ("Approved
   and sent: request R to ...", a Decline, or "rejected: <message>"). A message sent
   before the card was raised waits for the next turn, as the API told the member. A
   `closed` update whose turn failed or was refused never reached the transcript, so
   it goes again with the next turn (it doesn't wake one itself). A card raised in a
   chat that sends without review (`card_auto`) is approved by the workflow itself,
   with no Update and nobody to wait for; if the approval can't be stored, it is
   rejected with the reason and the turn resumes with "Couldn't send: ..." (3.10);
4. at an idle point, continue as new with `ConversationCarry` once the run has taken
   `turns_per_run` turns or Temporal suggests it (a message signalled meanwhile is
   carried, or handled first: queued work always runs before the continue); after
   `CONVERSATION_IDLE` with nothing to do and no update owed, return (the next message
   signal-with-starts it again).

An approved outreach card starts the `NetworkRequestWorkflow`: the update handler stores
the resolution (`NetworkActivities.apply_card_resolution`), and the run loop then runs
`start_network_request`, retrying until it starts, before it resumes the turn. When the
handler can't tell whether the resolution was stored (another got there first, or the
apply failed after its commit), the loop reads the card back (`settled_card`): a card
that isn't pending any more is resumed with what it settled to, so the chat never stays
paused on it. A failed or refused turn leaves `turn_failure` set (the chat says why
Claude couldn't reply: a spend cap, a model error, a refusal) until the next turn.

`resolve_card` may arrive while the turn that raised the card is still finishing (the
card row commits before the activity returns): the validator accepts it while a turn is
in flight, and the handler waits for the turn before checking the card id.

Owner: R-temporal.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.workflow import ActivityConfig

with workflow.unsafe.imports_passed_through():
    from harmony.agents.models import CardResolution
    from harmony.db.models.choices import FailureReason
    from harmony.temporal.activities.agents import (
        AgentActivities,
        CardOutcome,
        InternalTurnInput,
    )
    from harmony.temporal.activities.network import (
        CardResolutionApplied,
        CardResolutionInput,
        NetworkActivities,
        SettledCardInput,
        StartRequestInput,
    )
    from harmony.temporal.failures import failure_reason
    from harmony.temporal.policies import (
        WORLD_RETIRED,
        chat_activity_options,
        main_activity_options,
        mirror_activity_options,
    )
    from harmony.temporal.workflows.payloads import (
        CARD_NOT_PENDING_ERROR,
        CONVERSATION_IDLE,
        INVALID_RESOLUTION_ERROR,
        CardResolved,
        ConversationCarry,
        ConversationInput,
        ConversationPending,
        ConversationResult,
        MemberMessage,
        NetworkUpdate,
        update_failure,
    )


@dataclass(frozen=True, slots=True)
class _Paused:
    """A turn stopped on a card, resumed with the same id at `next_step`."""

    turn_id: UUID
    turn: int
    next_step: int
    card_id: UUID
    tool_use_id: str
    # Sent without review: the workflow settles it, never a member.
    auto: bool = False


@workflow.defn
class ConversationWorkflow:
    @workflow.init
    def __init__(self, conversation: ConversationInput) -> None:
        self._input = conversation
        self._queued_messages: list[MemberMessage] = list(conversation.carried.queued_messages)
        self._network_updates: list[NetworkUpdate] = list(conversation.carried.network_updates)
        self._turns = conversation.carried.turns
        carried = conversation.carried
        # The highest round of each request whose `closed` update was taken; ids carried
        # from before rounds count as round 1.
        self._closed_requests: dict[UUID, int] = {
            **dict.fromkeys(carried.closed_request_ids, 1),
            **carried.closed_request_rounds,
        }
        # Updates a failed or refused turn took, whose note may not have been stored
        # (a turn that failed after its first call stored them). They go with the next
        # turn without waking one, so a failure that keeps happening can't loop, and
        # that turn's activity drops any note the chat already holds.
        self._undelivered: list[NetworkUpdate] = list(conversation.carried.undelivered_updates)
        self._turn_failure: FailureReason | None = conversation.carried.turn_failure
        self._turn_in_flight = False
        self._pending_card_id: UUID | None = None
        self._paused: _Paused | None = None
        self._applied: CardResolutionApplied | None = None
        # The paused card may have been settled without this run knowing it.
        self._recheck = False
        self._resolving = False
        self._turns_this_run = 0

    @workflow.run
    async def run(self, conversation: ConversationInput) -> ConversationResult:
        while True:
            if self._paused is not None:
                await self._settle_card()
                continue
            if self._has_work():
                await self._new_turn()
                continue
            if (
                self._turns_this_run >= conversation.turns_per_run
                or workflow.info().is_continue_as_new_suggested()
            ):
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new(self._carry())
            try:
                await workflow.wait_condition(self._has_work, timeout=CONVERSATION_IDLE)
            except TimeoutError:
                await workflow.wait_condition(workflow.all_handlers_finished)
                # A note still owed to Claude would be lost with the run.
                if not self._has_work() and self._paused is None and not self._undelivered:
                    return ConversationResult(turns=self._turns)

    @workflow.signal
    def member_message(self, message: MemberMessage) -> None:
        """Queue a member message for the next turn. A message that answers the pending
        card (`card_id`) rejects it with this text as feedback instead (§12)."""
        self._queued_messages.append(message)

    @workflow.signal
    def network_update(self, update: NetworkUpdate) -> None:
        # Delivered at least once: a retried notice, or a second caller closing the same
        # round, must not wake Claude again.
        if self._closed_requests.get(update.request_id, 0) >= update.round:
            return
        self._closed_requests[update.request_id] = update.round
        self._network_updates.append(update)

    @workflow.update
    async def resolve_card(self, resolution: CardResolution) -> CardResolved:
        """Approve or reject the pending card and resume the paused turn. Returns once
        the resolution is stored, not when the request starts or the turn ends."""
        await workflow.wait_condition(lambda: self._paused is not None or not self._turn_in_flight)
        if not self._is_pending(resolution.card_id):
            raise ApplicationError(
                f"card {resolution.card_id} is not pending", type=CARD_NOT_PENDING_ERROR
            )
        _check_decision(resolution)
        self._resolving = True
        try:
            applied = await self._apply(resolution)
        except ApplicationError:
            self._recheck = True
            raise
        finally:
            self._resolving = False
        self._applied = applied
        return applied.resolved

    @resolve_card.validator
    def validate_resolve_card(self, resolution: CardResolution) -> None:
        """Reject (`ApplicationError` type `CARD_NOT_PENDING_ERROR`) unless
        `resolution.card_id` is the pending card; reject `feedback` on an approval
        (`INVALID_RESOLUTION_ERROR`)."""
        if self._paused is None and self._turn_in_flight:
            _check_decision(resolution)
            return
        if not self._is_pending(resolution.card_id):
            raise ApplicationError(
                f"card {resolution.card_id} is not pending", type=CARD_NOT_PENDING_ERROR
            )
        _check_decision(resolution)

    @workflow.query
    def pending(self) -> ConversationPending:
        return ConversationPending(
            queued_messages=list(self._queued_messages),
            network_updates=list(self._network_updates),
            turn_in_flight=self._turn_in_flight,
            pending_card_id=self._pending_card_id,
            turns=self._turns,
            turn_failure=self._turn_failure,
        )

    # --- The loop ---------------------------------------------------------------------

    def _has_work(self) -> bool:
        return bool(self._queued_messages or self._network_updates)

    async def _new_turn(self) -> None:
        messages, self._queued_messages = self._queued_messages, []
        updates = [*self._undelivered, *self._network_updates]
        self._undelivered, self._network_updates = [], []
        self._turns += 1
        self._turns_this_run += 1
        await self._run_turn(
            turn_id=workflow.uuid4(),
            turn=self._turns,
            first_step=0,
            messages=messages,
            updates=updates,
            card=None,
        )

    async def _settle_card(self) -> None:
        """Wait for the card's outcome (a message sent while it was pending rejects
        it), start an approved request, then resume. Returns without resuming when a
        read-back finds the card still pending."""
        paused = self._paused
        assert paused is not None
        await workflow.wait_condition(
            lambda: (
                self._applied is not None
                or (not self._resolving and (self._recheck or self._answer(paused) is not None))
            )
        )
        if self._applied is None and self._recheck:
            self._recheck = False
            self._applied = await self._settled(paused)
        elif self._applied is None:
            await self._reject_with_message(paused)
        applied = self._applied
        if applied is None:
            return
        self._applied = None
        self._paused = None
        self._pending_card_id = None
        # The turn resumes once the request has started: the chat reads busy until then
        # too, or the refetch the settled card prompts would find it idle and stop
        # following the resumed turn.
        self._turn_in_flight = True
        request_id = applied.resolved.request_id
        if applied.resolved.status == "approved" and request_id is not None:
            await self._start_request(request_id)
        await self._run_turn(
            turn_id=paused.turn_id,
            turn=paused.turn,
            first_step=paused.next_step,
            messages=[],
            updates=[],
            card=_outcome_of(applied, paused),
        )

    async def _reject_with_message(self, paused: _Paused) -> None:
        """A message sent while the card was pending rejects it, with the message as
        feedback."""
        message = self._queued_messages.pop(self._answer(paused) or 0)
        resolution = CardResolution(
            card_id=paused.card_id,
            decision="reject",
            member_id=message.member_id,
            feedback=message.text,
            rejected_with="message",
        )
        self._resolving = True
        try:
            # Nobody waits on this result, so an outage delays it rather than
            # inventing one.
            self._applied = await self._apply(resolution, options=mirror_activity_options())
        except ApplicationError as failed:
            if failed.type != CARD_NOT_PENDING_ERROR:
                self._queued_messages.insert(0, message)
                raise
            # Settled elsewhere first: resume with what it settled to, and the message
            # is the next turn's.
            self._applied = await self._settled(paused)
            self._queued_messages.insert(0, message.model_copy(update={"card_id": None}))
        finally:
            self._resolving = False

    async def _start_request(self, request_id: UUID) -> None:
        """Start the approved request's workflow. Its card is already approved, so an
        outage delays the start rather than leaving a request that never runs."""
        await workflow.execute_activity_method(
            NetworkActivities.start_network_request,
            StartRequestInput(world_id=self._input.world_id, request_id=request_id),
            **mirror_activity_options(),
        )

    async def _run_turn(
        self,
        *,
        turn_id: UUID,
        turn: int,
        first_step: int,
        messages: list[MemberMessage],
        updates: list[NetworkUpdate],
        card: CardOutcome | None,
    ) -> None:
        conversation = self._input
        self._turn_in_flight = True
        self._turn_failure = None
        try:
            result = await workflow.execute_activity_method(
                AgentActivities.internal_turn,
                InternalTurnInput(
                    world_id=conversation.world_id,
                    conversation_id=conversation.conversation_id,
                    org_id=conversation.org_id,
                    owner_member_id=conversation.owner_member_id,
                    cassette_thread=conversation.cassette_thread,
                    turn_id=turn_id,
                    turn=turn,
                    first_step=first_step,
                    messages=messages,
                    network_updates=updates,
                    card=card,
                ),
                **chat_activity_options(),
            )
        except ActivityError as failed:
            # The chat stays usable: it says why Claude couldn't reply, and the member's
            # next message starts a new turn.
            workflow.logger.warning("internal turn %s failed: %s", turn_id, failed)
            self._turn_failure = failure_reason(failed)
            self._undelivered = [*updates, *self._undelivered]
            return
        finally:
            self._turn_in_flight = False
        if result.stop == "refusal":
            self._turn_failure = "refused"
            self._undelivered = [*updates, *self._undelivered]
        if result.stop == "awaiting_card" and result.card_id is not None:
            paused = _Paused(
                turn_id=turn_id,
                turn=turn,
                next_step=result.next_step,
                card_id=result.card_id,
                tool_use_id=result.card_tool_use_id or "",
                auto=result.card_auto,
            )
            self._paused = paused
            if paused.auto:
                # Nobody needs to act: the chat stays busy through the send and the resume.
                self._turn_in_flight = True
                await self._send_without_review(paused)
            else:
                self._pending_card_id = result.card_id

    async def _send_without_review(self, paused: _Paused) -> None:
        """Approve a card the chat sends without review, as its owner, retrying until
        it is stored; `_settle_card` then starts the request and resumes the turn. An
        approval no retry can store (no recipient left, say) rejects the card with the
        reason instead, so the turn resumes knowing nothing went out. Only a retired
        world ends the run."""
        owner = self._input.owner_member_id
        self._resolving = True
        try:
            try:
                self._applied = await self._apply(
                    CardResolution(
                        card_id=paused.card_id, decision="approve", member_id=owner, auto=True
                    ),
                    options=mirror_activity_options(),
                )
            except ApplicationError as failed:
                if failed.type == WORLD_RETIRED:
                    raise
                workflow.logger.warning("sending card %s failed: %s", paused.card_id, failed)
                self._applied = await self._reject_unsent(paused, reason=failed.message)
        finally:
            self._resolving = False

    async def _reject_unsent(self, paused: _Paused, *, reason: str) -> CardResolutionApplied | None:
        resolution = CardResolution(
            card_id=paused.card_id,
            decision="reject",
            member_id=self._input.owner_member_id,
            feedback=reason,
            auto=True,
        )
        try:
            return await self._apply(resolution, options=mirror_activity_options())
        except ApplicationError as failed:
            if failed.type != CARD_NOT_PENDING_ERROR:
                raise
            return await self._settled(paused)

    def _is_pending(self, card_id: UUID) -> bool:
        """Whether `card_id` is the paused card, a member's to settle, and nothing has
        settled it yet."""
        paused = self._paused
        return (
            paused is not None
            and not paused.auto
            and paused.card_id == card_id
            and self._applied is None
            and not self._resolving
            and not self._recheck
        )

    def _answer(self, paused: _Paused) -> int | None:
        """The first queued message sent while this card was pending."""
        return next(
            (
                index
                for index, message in enumerate(self._queued_messages)
                if message.card_id == paused.card_id
            ),
            None,
        )

    async def _settled(self, paused: _Paused) -> CardResolutionApplied | None:
        """What the card settled to, read back; None while it is still pending."""
        conversation = self._input
        self._resolving = True
        try:
            return await workflow.execute_activity_method(
                NetworkActivities.settled_card,
                SettledCardInput(
                    world_id=conversation.world_id,
                    conversation_id=conversation.conversation_id,
                    card_id=paused.card_id,
                ),
                **mirror_activity_options(),
            )
        finally:
            self._resolving = False

    async def _apply(
        self, resolution: CardResolution, *, options: ActivityConfig | None = None
    ) -> CardResolutionApplied:
        conversation = self._input
        try:
            return await workflow.execute_activity_method(
                NetworkActivities.apply_card_resolution,
                CardResolutionInput(
                    world_id=conversation.world_id,
                    conversation_id=conversation.conversation_id,
                    resolution=resolution,
                    request_id=workflow.uuid4(),
                ),
                **(options or main_activity_options()),
            )
        except ActivityError as failed:
            raise update_failure(failed) from failed

    def _carry(self) -> ConversationInput:
        return self._input.model_copy(
            update={
                "carried": ConversationCarry(
                    queued_messages=list(self._queued_messages),
                    network_updates=list(self._network_updates),
                    turns=self._turns,
                    closed_request_rounds=dict(sorted(self._closed_requests.items())),
                    undelivered_updates=list(self._undelivered),
                    turn_failure=self._turn_failure,
                )
            }
        )


def _outcome_of(applied: CardResolutionApplied, paused: _Paused) -> CardOutcome:
    return CardOutcome(
        card_id=paused.card_id,
        tool_use_id=applied.tool_use_id or paused.tool_use_id,
        content=applied.content,
        is_error=applied.is_error,
    )


def _check_decision(resolution: CardResolution) -> None:
    if resolution.decision == "approve" and resolution.feedback:
        raise ApplicationError("feedback goes with a rejection", type=INVALID_RESOLUTION_ERROR)


__all__ = ["ConversationWorkflow"]
