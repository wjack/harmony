"""Network bookkeeping: the short database activities on `harmony-main` (§7).

Each writes through one `UnitOfWork` (the domain services in `harmony.domain`), is
idempotent on the IDs its caller minted (`workflow.uuid4()`), and checks the world is
live first. Activities that start or signal other workflows go through
`start_world_workflow` (signal-with-start, `USE_EXISTING`), so the started workflow is
recorded for retirement.

Run options: `policies.main_activity_options()`.

Owner: R-temporal.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from harmony.agents import consult as answerer
from harmony.agents import context
from harmony.agents.keys import chat_key
from harmony.agents.models import CardResolution, EscalationResolution, OutreachRecipient
from harmony.agents.runtime import OwedResult
from harmony.agents.tools.internal import CardChanges, declined_result, sent_result
from harmony.agents.transcript import TranscriptRef, take_back_in
from harmony.db import repo
from harmony.db.models import (
    Card,
    Conversation,
    Escalation,
    EscalationDraft,
    Member,
    NetworkRequest,
    Org,
    Thread,
)
from harmony.db.models.choices import EndReason, FailureReason, Side, SteerFailure, ThreadState
from harmony.db.uow import UnitOfWork, unit_of_work
from harmony.domain import (
    cards,
    consults,
    conversations,
    escalations,
    notes,
    reopens,
    requests,
    steers,
    threads,
)
from harmony.domain.errors import (
    CardNotPending,
    ConsultNotOpen,
    EscalationNotOpen,
    InvalidRoute,
    NotAdmin,
    NotInOrg,
    ReopenConflict,
    SteerConflict,
)
from harmony.temporal.policies import ensure_world_live
from harmony.temporal.start import start_world_workflow
from harmony.temporal.workflows.payloads import (
    CardResolved,
    ConversationCarry,
    ConversationInput,
    EscalationResolved,
    NetworkNews,
    NetworkRequestInput,
    NetworkUpdate,
    Reassigned,
    Reassignment,
    ReopenInput,
    SteerRequest,
    ThreadCarry,
    ThreadInput,
    conversation_workflow_id,
    request_workflow_id,
    thread_workflow_id,
)

# Errors no retry can fix: they reach the workflow (and the API) as non-retryable
# `ApplicationError`s typed by class name (`payloads.*_ERROR`).
DOMAIN_ERRORS: Final = (
    CardNotPending,
    ConsultNotOpen,
    EscalationNotOpen,
    InvalidRoute,
    NotAdmin,
    NotInOrg,
    ReopenConflict,
    SteerConflict,
    repo.NotFoundInWorld,
    ValueError,
)
# Every thread of a request ends by `created_at + REQUEST_LIFETIME` (its `deadline_at`),
# well inside the 24 h execution timeout (`start.WORLD_RUN_TIMEOUT`), so the workflows
# write their own terminal states and the request closes.
REQUEST_LIFETIME: Final = timedelta(hours=20)


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CardResolutionInput(_Payload):
    world_id: UUID
    conversation_id: UUID
    resolution: CardResolution
    # Minted by the workflow for an approved outreach card.
    request_id: UUID


class SettledCardInput(_Payload):
    world_id: UUID
    conversation_id: UUID
    card_id: UUID


class CardResolutionApplied(_Payload):
    resolved: CardResolved
    # The `tool_result` the paused internal turn resumes with.
    tool_use_id: str
    content: str
    is_error: bool = False


class StartRequestInput(_Payload):
    world_id: UUID
    request_id: UUID


class OpenThreadInput(_Payload):
    world_id: UUID
    request_id: UUID
    thread_id: UUID
    responder_org_id: UUID


class ApplyResolutionInput(_Payload):
    world_id: UUID
    thread_id: UUID
    resolution: EscalationResolution
    # Minted by the workflow: the message a `take_over` sends.
    message_id: UUID


class ResolutionApplied(_Payload):
    resolved: EscalationResolved
    # The owed tool result of the call that opened the item, now known: the consult's
    # answer, or a held message's outcome. Its `tool_use_id` is the workflow's to set.
    owed: OwedResult | None
    side: Side
    # A message reached the counterparty because of the resolution (a held message
    # approved, or a member's own): the thread moves on as after that send.
    sent: bool = False


class ReassignInput(_Payload):
    world_id: UUID
    reassignment: Reassignment


class RecordSteerInput(_Payload):
    world_id: UUID
    thread_id: UUID
    steer: SteerRequest
    # The rev reserved for the rewrite of the steer's item, if it has one.
    draft_rev: int | None = None


class SteerRecorded(_Payload):
    # The steer's per-(thread, side) `seq`.
    seq: int
    # Its author's name, for the rewrite's prompt.
    author_name: str


class SettleSteerInput(_Payload):
    world_id: UUID
    steer_id: UUID
    # Why a part didn't land, as the workflow saw it: a failed activity's reason, or
    # `thread_closed` when the thread ended first.
    failure: SteerFailure | None = None


class ThreadStateInput(_Payload):
    world_id: UUID
    thread_id: UUID
    state: ThreadState
    side_to_move: Side | None = None
    # How the run ended (REOPEN.md §2.1): required with a terminal state.
    end_reason: EndReason | None = None
    # The side whose representative ended it (a close, a refusal), stored with it.
    closed_by: Side | None = None
    # When this call ends the thread, `closed_by`'s counterparty is queued the `closed`
    # notice for its last turn (a refusal after something crossed). A `close` queues its
    # own.
    last_turn: bool = False
    # Why the system ended it, when it did (3.5): stored with the terminal state.
    stop_reason: FailureReason | None = None


class UnsummarizedInput(_Payload):
    world_id: UUID
    thread_id: UUID
    org_id: UUID
    # The last turn that failed: it names the note, so a retry writes it once.
    turn_id: UUID
    reason: FailureReason


class RecordReopenInput(_Payload):
    world_id: UUID
    thread_id: UUID
    reopen: ReopenInput


class BeginReopenInput(_Payload):
    world_id: UUID
    thread_id: UUID
    # The run it starts, with what it starts with.
    run: int
    steer_id: UUID
    deadline_at: datetime | None
    max_turns: int


class FinishRunInput(_Payload):
    world_id: UUID
    thread_id: UUID
    run: int
    carry: ThreadCarry


class EndRoundInput(_Payload):
    world_id: UUID
    request_id: UUID
    # The request workflow's tail, which has seen every first run end.
    from_workflow: bool


class RoundEnded(_Payload):
    """`end_round`'s result: whether the request is closed, in which round, and the
    chat to tell when it closed."""

    closed: bool
    round: int
    conversation_id: UUID | None


class NotifyConversationInput(_Payload):
    world_id: UUID
    conversation_id: UUID
    update: NetworkUpdate


class NudgeChatInput(_Payload):
    world_id: UUID
    request_id: UUID


class TakeBackInput(_Payload):
    transcript: TranscriptRef
    turn_id: UUID


class NetworkActivities:
    """Database activities (queue `harmony-main`)."""

    def __init__(self, client: Client, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._client = client
        self._sessionmaker = sessionmaker

    @activity.defn
    async def apply_card_resolution(self, card: CardResolutionInput) -> CardResolutionApplied:
        """`cards.resolve`, and for an approval `requests.create_from_card` (the
        workflow then calls `start_network_request`). Raises `CardNotPending`."""
        await ensure_world_live(self._sessionmaker, card.world_id)
        resolution = card.resolution
        async with domain_errors(), self._sessionmaker() as session:
            row = await repo.get_required(session, Card, card.world_id, resolution.card_id)
            if row.conversation_id != card.conversation_id:
                raise repo.NotFoundInWorld(Card, card.world_id, resolution.card_id)
            if row.status != "pending":
                if cards.resolution_of(row) != resolution:
                    raise CardNotPending(row.id, row.status)
                # A retry after the commit: report what was applied.
                return await _card_applied(session, row, card.request_id)
        async with domain_errors(), unit_of_work(self._sessionmaker, card.world_id) as uow:
            resolved = await cards.resolve(uow, resolution)
            if resolution.decision == "approve":
                await requests.create_from_card(
                    uow, resolved, resolution, request_id=card.request_id
                )
            await uow.commit()
        async with self._sessionmaker() as session:
            row = await repo.get_required(session, Card, card.world_id, resolution.card_id)
            return await _card_applied(session, row, card.request_id)

    @activity.defn
    async def settled_card(self, card: SettledCardInput) -> CardResolutionApplied | None:
        """What a card settled to, reported as `apply_card_resolution` reports it; None
        while it is still pending. The workflow reads a card back when it can't tell
        whether a resolution was stored (another resolution got there first, or the
        apply failed after its commit)."""
        await ensure_world_live(self._sessionmaker, card.world_id)
        async with domain_errors(), self._sessionmaker() as session:
            row = await repo.get_required(session, Card, card.world_id, card.card_id)
            if row.conversation_id != card.conversation_id:
                raise repo.NotFoundInWorld(Card, card.world_id, card.card_id)
            if row.status == "pending":
                return None
            return await _card_applied(session, row, None)

    @activity.defn
    async def start_network_request(self, start: StartRequestInput) -> None:
        """Start `NetworkRequestWorkflow` for a created request (USE_EXISTING)."""
        await start_request_workflow(
            self._client, self._sessionmaker, world_id=start.world_id, request_id=start.request_id
        )

    @activity.defn
    async def open_thread(self, thread: OpenThreadInput) -> ThreadInput:
        """`threads.open`, then start its `ThreadWorkflow` (USE_EXISTING). Returns the
        input the thread was started with (its cassette key fixed here)."""
        from harmony.temporal.workflows.thread import ThreadWorkflow

        await ensure_world_live(self._sessionmaker, thread.world_id)
        async with domain_errors(), unit_of_work(self._sessionmaker, thread.world_id) as uow:
            request = await repo.get_required(
                uow.session, NetworkRequest, thread.world_id, thread.request_id
            )
            await threads.open(
                uow,
                thread_id=thread.thread_id,
                request=request,
                responder_org_id=thread.responder_org_id,
                deadline_at=request.created_at + REQUEST_LIFETIME,
            )
            await uow.commit()
        async with self._sessionmaker() as session:
            request = await repo.get_required(
                session, NetworkRequest, thread.world_id, thread.request_id
            )
            row = await repo.get_required(session, Thread, thread.world_id, thread.thread_id)
            arg = ThreadInput(
                world_id=thread.world_id,
                thread_id=row.id,
                request_id=request.id,
                requester_org_id=row.requester_org_id,
                responder_org_id=row.responder_org_id,
                cassette_thread=await threads.cassette_key(session, row),
                deadline_at=row.deadline_at,
            )
        await start_world_workflow(
            self._client,
            self._sessionmaker,
            ThreadWorkflow.run,
            arg,
            world_id=thread.world_id,
            workflow_id=thread_workflow_id(thread.world_id, thread.thread_id),
            org_id=arg.requester_org_id,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        return arg

    @activity.defn
    async def apply_resolution(self, resolution: ApplyResolutionInput) -> ResolutionApplied:
        """`escalations.resolve` and its effect: the consult's answer released to this
        thread (or declined), the held message sent, or the member's own message. Raises
        `EscalationNotOpen` / `NotInOrg`."""
        await ensure_world_live(self._sessionmaker, resolution.world_id)
        member_resolution = resolution.resolution
        async with domain_errors(), self._sessionmaker() as session:
            escalation = await repo.get_required(
                session, Escalation, resolution.world_id, member_resolution.escalation_id
            )
            if escalation.thread_id != resolution.thread_id:
                raise repo.NotFoundInWorld(
                    Escalation, resolution.world_id, member_resolution.escalation_id
                )
            stored = escalation.resolution
            if escalation.status == "resolved" and stored is not None:
                if EscalationResolution.model_validate(stored) != member_resolution:
                    raise _not_open(escalation)
                # A retry after the commit: the effects are in; rebuild the result.
                return await _resolution_result(session, escalation, resolution)
            if escalation.status != "open":
                raise _not_open(escalation)
        async with domain_errors(), unit_of_work(self._sessionmaker, resolution.world_id) as uow:
            escalation = await escalations.resolve(uow, member_resolution)
            await _apply_effects(uow, escalation, resolution)
            await uow.commit()
        async with self._sessionmaker() as session:
            escalation = await repo.get_required(
                session, Escalation, resolution.world_id, member_resolution.escalation_id
            )
            return await _resolution_result(session, escalation, resolution)

    @activity.defn
    async def stored_resolution(self, resolution: ApplyResolutionInput) -> ResolutionApplied | None:
        """What `apply_resolution` stored for this escalation, rebuilt as it reports it;
        None while the escalation is open (nothing was stored). The thread reads it back
        when an apply failed, since it may have failed after its commit."""
        await ensure_world_live(self._sessionmaker, resolution.world_id)
        async with domain_errors(), self._sessionmaker() as session:
            escalation = await repo.get_required(
                session, Escalation, resolution.world_id, resolution.resolution.escalation_id
            )
            stored = escalation.resolution
            if escalation.status != "resolved" or stored is None:
                return None
            applied = resolution.model_copy(
                update={"resolution": EscalationResolution.model_validate(stored)}
            )
            return await _resolution_result(session, escalation, applied)

    @activity.defn
    async def reassign_escalation(self, reassign: ReassignInput) -> Reassigned:
        await ensure_world_live(self._sessionmaker, reassign.world_id)
        change = reassign.reassignment
        async with domain_errors(), unit_of_work(self._sessionmaker, reassign.world_id) as uow:
            escalation = await escalations.reassign(
                uow,
                change.escalation_id,
                to_member_ids=[change.to_member_id],
                member_id=change.by_member_id,
            )
            assignees = escalations.assignee_ids(escalation)
            await uow.commit()
        return Reassigned(escalation_id=change.escalation_id, assignees=assignees)

    @activity.defn
    async def record_steer(self, record: RecordSteerInput) -> SteerRecorded:
        """`steers.record` for an Edit: stored `working` at the side's next `seq`.
        Idempotent; a different body under the same id raises `SteerConflict`."""
        await ensure_world_live(self._sessionmaker, record.world_id)
        steer = record.steer
        async with domain_errors(), unit_of_work(self._sessionmaker, record.world_id) as uow:
            row = await steers.record(
                uow,
                steer_id=steer.steer_id,
                thread_id=record.thread_id,
                org_id=steer.org_id,
                kind="edit",
                author_member_id=steer.member_id,
                body=steer.body,
                escalation_id=steer.escalation_id,
                draft_text=steer.draft_text,
                draft_rev=record.draft_rev,
            )
            author = await repo.get_required(uow.session, Member, uow.world_id, steer.member_id)
            recorded = SteerRecorded(seq=row.seq, author_name=author.name)
            await uow.commit()
        return recorded

    @activity.defn
    async def settle_steer(self, settle: SettleSteerInput) -> int | None:
        """`steers.settle`: the steer's outcome, read from the rows its parts wrote. A
        settled steer, or one that was never stored, is left alone. Returns the steer's
        `seq`, None when it was never stored."""
        await ensure_world_live(self._sessionmaker, settle.world_id)
        async with domain_errors(), unit_of_work(self._sessionmaker, settle.world_id) as uow:
            steer = await steers.settle(uow, settle.steer_id, failure=settle.failure)
            seq = steer.seq if steer is not None else None
            await uow.commit()
        return seq

    @activity.defn
    async def set_thread_state(self, state: ThreadStateInput) -> None:
        """Move the thread. An ended thread's open escalations are cancelled, and when
        this call is what ends it for a `last_turn`, `closed_by`'s counterparty is
        queued the `closed` notice for that turn."""
        await ensure_world_live(self._sessionmaker, state.world_id)
        async with domain_errors(), unit_of_work(self._sessionmaker, state.world_id) as uow:
            thread = await threads.locked(uow, state.thread_id)
            # Only on the transition itself, so a retry after the commit (or a thread a
            # `close` already ended) queues nothing.
            ending = (
                thread.state not in threads.TERMINAL_STATES
                and state.state in threads.TERMINAL_STATES
            )
            thread = await threads.set_state(
                uow,
                state.thread_id,
                state.state,
                side_to_move=state.side_to_move,
                end_reason=state.end_reason,
                closed_by=state.closed_by,
                stop_reason=state.stop_reason,
            )
            if thread.state in threads.TERMINAL_STATES:
                await _cancel_open_items(uow, thread)
            if ending and state.last_turn and state.closed_by is not None:
                await _queue_closed_notice(uow, thread, state.closed_by)
            await uow.commit()

    @activity.defn
    async def note_unsummarized(self, failed: UnsummarizedInput) -> None:
        """`notes.add_unsummarized`: why a side's last turn wrote no closing summary."""
        await ensure_world_live(self._sessionmaker, failed.world_id)
        async with domain_errors(), unit_of_work(self._sessionmaker, failed.world_id) as uow:
            await notes.add_unsummarized(
                uow,
                thread_id=failed.thread_id,
                org_id=failed.org_id,
                turn_id=failed.turn_id,
                reason=failed.reason,
            )
            await uow.commit()

    @activity.defn
    async def record_reopen(self, record: RecordReopenInput) -> int:
        """`steers.record` for a reopen, as the API recorded it (a commit that failed
        after the start leaves it to this). Idempotent; returns the steer's `seq`."""
        await ensure_world_live(self._sessionmaker, record.world_id)
        reopen = record.reopen
        async with domain_errors(), unit_of_work(self._sessionmaker, record.world_id) as uow:
            row = await steers.record(
                uow,
                steer_id=reopen.steer_id,
                thread_id=record.thread_id,
                org_id=reopen.org_id,
                kind="reopen",
                author_member_id=reopen.member_id,
                body=reopen.body,
            )
            seq = row.seq
            await uow.commit()
        return seq

    @activity.defn
    async def begin_reopen(self, begin: BeginReopenInput) -> int:
        """`reopens.begin`: the thread live again as run `run`, in one transaction once
        its guidance landed (REOPEN.md §4.4). Returns the run's `start_seq`. Raises
        `ReopenConflict` when the thread isn't ready for it."""
        await ensure_world_live(self._sessionmaker, begin.world_id)
        async with domain_errors(), unit_of_work(self._sessionmaker, begin.world_id) as uow:
            start_seq = await reopens.begin(
                uow,
                thread_id=begin.thread_id,
                run=begin.run,
                steer_id=begin.steer_id,
                deadline_at=begin.deadline_at,
                max_turns=begin.max_turns,
            )
            await uow.commit()
        return start_seq

    @activity.defn
    async def finish_run(self, finish: FinishRunInput) -> None:
        """`reopens.finish_run`: every run's last activity (REOPEN.md §4.6)."""
        await ensure_world_live(self._sessionmaker, finish.world_id)
        async with domain_errors(), unit_of_work(self._sessionmaker, finish.world_id) as uow:
            await reopens.finish_run(
                uow, finish.thread_id, finish.run, finish.carry.model_dump(mode="json")
            )
            await uow.commit()

    @activity.defn
    async def end_round(self, end: EndRoundInput) -> RoundEnded:
        """`requests.end_round`: close the request once nothing on it is live."""
        await ensure_world_live(self._sessionmaker, end.world_id)
        async with domain_errors(), unit_of_work(self._sessionmaker, end.world_id) as uow:
            ended = await requests.end_round(uow, end.request_id, from_workflow=end.from_workflow)
            await uow.commit()
        return RoundEnded(
            closed=ended.closed, round=ended.round, conversation_id=ended.conversation_id
        )

    @activity.defn
    async def notify_conversation(self, notify: NotifyConversationInput) -> None:
        """Signal-with-start the origin `ConversationWorkflow`'s `network_update`."""
        from harmony.temporal.workflows.conversation import ConversationWorkflow

        await ensure_world_live(self._sessionmaker, notify.world_id)
        async with domain_errors(), unit_of_work(self._sessionmaker, notify.world_id) as uow:
            await conversations.touch(uow, notify.conversation_id)
            await uow.commit()
        async with self._sessionmaker() as session:
            conversation = await repo.get_required(
                session, Conversation, notify.world_id, notify.conversation_id
            )
            arg = await conversation_input(session, conversation)
        await start_world_workflow(
            self._client,
            self._sessionmaker,
            ConversationWorkflow.run,
            arg,
            world_id=notify.world_id,
            workflow_id=conversation_workflow_id(notify.world_id, notify.conversation_id),
            org_id=conversation.org_id,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            start_signal="network_update",
            start_signal_args=[notify.update],
        )

    @activity.defn
    async def nudge_chat(self, nudge: NudgeChatInput) -> None:
        """Tell the request's chat there's news (`network_news`, signal-with-start): a
        counterparty wrote, or a conversation ended. One read, no write: the chat looks
        later, so the item list needs no refetch now."""
        await ensure_world_live(self._sessionmaker, nudge.world_id)
        await nudge_request_chat(
            self._client,
            self._sessionmaker,
            world_id=nudge.world_id,
            news=NetworkNews(request_id=nudge.request_id),
        )

    @activity.defn
    async def take_back_turn(self, take: TakeBackInput) -> bool:
        """Delete a refused turn's stored input (`transcript.take_back_in`), so a Retry
        sends the same input again as a new turn. False when it can't be: something
        answered it, or came after it."""
        world_id = take.transcript.world_id
        await ensure_world_live(self._sessionmaker, world_id)
        async with domain_errors(), unit_of_work(self._sessionmaker, world_id) as uow:
            taken = await take_back_in(uow, take.transcript, take.turn_id)
            await uow.commit()
        return taken


# --- Shared with the API -------------------------------------------------------------


async def nudge_request_chat(
    client: Client,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    world_id: UUID,
    news: NetworkNews,
) -> None:
    """Signal-with-start `network_news` on the chat the request came from, if any."""
    # Imported here: the workflow module imports this one for its activity types.
    from harmony.temporal.workflows.conversation import ConversationWorkflow

    async with sessionmaker() as session:
        request = await repo.get_required(session, NetworkRequest, world_id, news.request_id)
        if request.conversation_id is None:
            return
        conversation = await repo.get_required(
            session, Conversation, world_id, request.conversation_id
        )
        arg = await conversation_input(session, conversation)
    await start_world_workflow(
        client,
        sessionmaker,
        ConversationWorkflow.run,
        arg,
        world_id=world_id,
        workflow_id=conversation_workflow_id(world_id, conversation.id),
        org_id=conversation.org_id,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal="network_news",
        start_signal_args=[news],
    )


async def start_request_workflow(
    client: Client,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    world_id: UUID,
    request_id: UUID,
) -> None:
    """Start `NetworkRequestWorkflow` for an approved request (USE_EXISTING): one an
    outreach card created, or one the world was seeded with."""
    # Imported here: the workflow module imports this one for its activity types.
    from harmony.temporal.workflows.network_request import NetworkRequestWorkflow

    async with sessionmaker() as session:
        request = await repo.get_required(session, NetworkRequest, world_id, request_id)
        arg = NetworkRequestInput(
            world_id=world_id,
            request_id=request.id,
            requester_org_id=request.requester_org_id,
            conversation_id=request.conversation_id,
            recipient_org_ids=[r.org_id for r in requests.recipients_of(request)],
        )
    await start_world_workflow(
        client,
        sessionmaker,
        NetworkRequestWorkflow.run,
        arg,
        world_id=world_id,
        workflow_id=request_workflow_id(world_id, request.id),
        org_id=request.requester_org_id,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )


async def conversation_input(
    session: AsyncSession, conversation: Conversation
) -> ConversationInput:
    """The input a conversation's workflow (re)starts with: its cassette key and the
    turns and looks it has already taken, so cassette turn numbers continue after an
    idle exit."""
    owner = await repo.get_required(
        session, Member, conversation.world_id, conversation.owner_member_id
    )
    return ConversationInput(
        world_id=conversation.world_id,
        conversation_id=conversation.id,
        org_id=conversation.org_id,
        owner_member_id=conversation.owner_member_id,
        cassette_thread=chat_key(owner.slug, await conversations.ordinal(session, conversation)),
        carried=ConversationCarry(
            turns=await conversations.turns_taken(session, conversation),
            looks=await conversations.looks_taken(session, conversation),
        ),
    )


async def run_update[R](
    handle: WorkflowHandle[Any, Any],
    update: str,
    arg: BaseModel,
    result_type: type[R],
    *,
    id: str | None = None,
) -> R:
    """`execute_update` by name with a typed result (the SDK's overloads leave the
    by-name form partly untyped). An `id` makes a repeated call the same Update."""
    result: R = await handle.execute_update(  # pyright: ignore[reportUnknownMemberType]
        update, arg, id=id, result_type=result_type
    )
    return result


async def run_query[R](
    handle: WorkflowHandle[Any, Any], query: str, result_type: type[R], *, rpc_timeout: timedelta
) -> R:
    """`query` by name with a typed result."""
    result: R = await handle.query(  # pyright: ignore[reportUnknownMemberType]
        query, result_type=result_type, rpc_timeout=rpc_timeout
    )
    return result


def workflow_missing(error: RPCError) -> bool:
    """An Update or query aimed at a workflow that isn't running (never started, as for
    seeded threads, or already closed)."""
    return error.status in (RPCStatusCode.NOT_FOUND, RPCStatusCode.FAILED_PRECONDITION)


# --- Cards ---------------------------------------------------------------------------


async def _card_applied(
    session: AsyncSession, card: Card, request_id: UUID | None
) -> CardResolutionApplied:
    """The resolved card as the paused turn's `tool_result`. A card the chat sent
    without review says so (3.10): Claude learns the send went out unreviewed, or why
    nothing went out. A member's Decline reads as a decision (`declined_result`); a
    message typed while the card waited replaced it, and reads as that message."""
    resolution = cards.resolution_of(card)
    if card.status == "rejected" or resolution is None:
        feedback = resolution.feedback if resolution is not None else None
        if card.auto:
            return CardResolutionApplied(
                resolved=CardResolved(card_id=card.id, status=card.status),
                tool_use_id=card.tool_use_id,
                content=f"Couldn't send: {feedback or 'it was refused'}. Nothing went out.",
                is_error=True,
            )
        if resolution is not None and resolution.rejected_with == "decline":
            member = await _member_name(session, card.world_id, resolution.member_id)
            content = declined_result(member=member, reason=feedback)
        else:
            content = f"rejected: {feedback}" if feedback else "rejected"
        return CardResolutionApplied(
            resolved=CardResolved(card_id=card.id, status=card.status),
            tool_use_id=card.tool_use_id,
            content=content,
        )
    request = await session.scalar(
        repo.in_world(NetworkRequest, card.world_id).where(NetworkRequest.card_id == card.id)
    )
    request_id = request.id if request is not None else request_id
    if request_id is None:
        raise LookupError(f"approved card {card.id} has no request")
    sent = requests.recipients_of(request) if request is not None else []
    title = threads.requester_title(request) if request is not None else None
    return CardResolutionApplied(
        resolved=CardResolved(card_id=card.id, status=card.status, request_id=request_id),
        tool_use_id=card.tool_use_id,
        content=sent_result(
            request_id=request_id,
            title=title or cards.payload_of(card).title or "",
            orgs=await _org_names(session, card.world_id, [r.org_id for r in sent]),
            # The chat's owner: the member who approved it, or whose chat sent it.
            member=await _member_name(session, card.world_id, resolution.member_id),
            auto=card.auto,
            changes=await _card_changes(session, card, resolution, sent),
        ),
    )


async def _card_changes(
    session: AsyncSession,
    card: Card,
    resolution: CardResolution,
    sent: Sequence[OutreachRecipient],
) -> CardChanges:
    """What went out differently from the card Claude proposed: the organizations listed
    but not sent to, and the brief and asks the member rewrote."""
    payload = cards.payload_of(card)
    proposed = {row.org_id: row.ask for row in payload.recipients}
    sent_ids = {row.org_id for row in sent}
    unsent = [row.org_id for row in payload.recipients if row.org_id not in sent_ids]
    asked = [
        (row.org_id, row.ask)
        for row in sent
        if row.ask is not None
        and row.org_id in proposed
        and row.ask.strip() != (proposed[row.org_id] or "").strip()
    ]
    edits = resolution.edits
    brief = edits.brief if edits is not None else None
    rows = await repo.list_in_world(
        session, Org, card.world_id, Org.id.in_([*unsent, *(org_id for org_id, _ in asked)])
    )
    names = {org.id: org.name for org in rows}
    return CardChanges(
        unsent=[names[org_id] for org_id in unsent if org_id in names],
        brief=brief if brief is not None and brief.strip() != payload.brief.strip() else None,
        asks=[(names[org_id], ask) for org_id, ask in asked if org_id in names],
    )


# --- Resolutions ---------------------------------------------------------------------


async def _apply_effects(
    uow: UnitOfWork, escalation: Escalation, applied: ApplyResolutionInput
) -> None:
    session = uow.session
    thread = await repo.get_required(session, Thread, uow.world_id, applied.thread_id)
    side = escalation.side or threads.side_of(thread, escalation.org_id)
    if escalation.kind == "outbound_review":
        await _apply_held(uow, escalation, thread, applied, side)
        return
    if escalation.consult_id is None:
        return
    await _settle_consult(uow, escalation, escalation.consult_id, thread, applied)


async def _settle_consult(
    uow: UnitOfWork,
    escalation: Escalation,
    consult_id: UUID,
    thread: Thread,
    applied: ApplyResolutionInput,
) -> None:
    """A member's answer to a consult: released to this one thread (§4.5), declined, or
    replaced by the member writing to the counterparty themselves."""
    resolution = applied.resolution
    text = (resolution.text or "").strip()
    match resolution.action:
        case "approve":
            draft = await escalations.resolved_draft(uow.session, escalation, resolution)
            answer = text or (draft.answer if draft is not None else "")
            if not answer:
                raise ValueError("an approval needs the answer to release")
            await consults.release(
                uow,
                consult_id,
                text=answerer.result_text("approve", answer),
                released_by="member",
                member_id=resolution.member_id,
            )
        case "decline":
            await consults.decline(
                uow,
                consult_id,
                text=answerer.result_text("decline", text),
                member_id=resolution.member_id,
            )
        case "take_over":
            await threads.append_message(
                uow,
                thread.id,
                message_id=applied.message_id,
                from_org_id=escalation.org_id,
                kind="message",
                body=text,
                provenance="human",
                author_member_id=resolution.member_id,
            )
            await consults.release(
                uow,
                consult_id,
                text=answerer.result_text("take_over", text),
                released_by="member",
                member_id=resolution.member_id,
            )


async def _apply_held(
    uow: UnitOfWork,
    escalation: Escalation,
    thread: Thread,
    applied: ApplyResolutionInput,
    side: Side,
) -> None:
    """A message held for a person: approved, the rev the member resolved goes out (as
    they edited it, if they did) under `escalations.held_message_id`, provenance
    `agent_approved`, stamped with the representative's turn (rev 1's: a later rev is
    a steer's rewrite); `take_over` sends the member's own message instead; `decline`
    sends nothing."""
    resolution = applied.resolution
    text = (resolution.text or "").strip()
    match resolution.action:
        case "approve":
            draft = await escalations.resolved_draft(uow.session, escalation, resolution)
            written = text or (draft.answer if draft is not None else "")
            if not written:
                raise ValueError("an approval needs the message to send")
            drafted = await escalations.first_draft(uow.session, escalation)
            await threads.append_message(
                uow,
                thread.id,
                message_id=escalations.held_message_id(escalation),
                from_org_id=escalation.org_id,
                kind="message",
                body=written,
                provenance="agent_approved",
                turn_id=drafted.turn_id if drafted is not None else None,
                approved_by_member_id=resolution.member_id,
            )
        case "take_over":
            await threads.append_message(
                uow,
                thread.id,
                message_id=applied.message_id,
                from_org_id=escalation.org_id,
                kind="message",
                body=text,
                provenance="human",
                author_member_id=resolution.member_id,
            )
        case "decline":
            pass


async def _resolution_result(
    session: AsyncSession, escalation: Escalation, applied: ApplyResolutionInput
) -> ResolutionApplied:
    resolution = applied.resolution
    thread = await repo.get_required(session, Thread, escalation.world_id, applied.thread_id)
    side = escalation.side or threads.side_of(thread, escalation.org_id)
    owed: OwedResult | None = None
    sent = False
    if escalation.consult_id is not None:
        consult = await consults.get(
            session, world_id=escalation.world_id, consult_id=escalation.consult_id
        )
        # The same text `consult` owes for a question asked again after it settled.
        owed = OwedResult(
            tool_use_id=consult.tool_use_id or "", content=consult.released_text or ""
        )
        sent = resolution.action == "take_over"
    elif escalation.kind == "outbound_review":
        # The representative's own text: what crossed is compared with what it wrote.
        owed, sent = _held_outcome(
            resolution,
            await escalations.first_draft(session, escalation),
            await escalations.resolved_draft(session, escalation, resolution),
        )
    return ResolutionApplied(
        resolved=EscalationResolved(
            escalation_id=escalation.id,
            action=resolution.action,
            message_id=_sent_message_id(escalation, resolution, applied.message_id),
        ),
        owed=owed,
        side=side,
        sent=sent,
    )


def _held_outcome(
    resolution: EscalationResolution,
    drafted: EscalationDraft | None,
    resolved: EscalationDraft | None,
) -> tuple[OwedResult, bool]:
    """A resolved hold's result for the representative, against what it wrote
    (`drafted`, rev 1), and whether a message reached the counterparty. `resolved` is
    the rev the member resolved."""
    content = answerer.held_result(
        resolution.action,
        text=resolution.text or "",
        drafted=drafted.answer if drafted is not None else "",
        resolved=resolved.answer if resolved is not None else "",
    )
    unsent = resolution.action == "decline"
    return OwedResult(tool_use_id="", content=content, is_error=unsent), not unsent


def _sent_message_id(
    escalation: Escalation, resolution: EscalationResolution, take_over_id: UUID
) -> UUID | None:
    match resolution.action:
        case "take_over":
            return take_over_id
        case "approve" if escalation.kind == "outbound_review":
            return escalations.held_message_id(escalation)
        case _:
            return None


# --- Thread endings ------------------------------------------------------------------


async def _cancel_open_items(uow: UnitOfWork, thread: Thread) -> None:
    """Nobody can act on an ended thread's open items any more. Drafts stay drafts:
    they were never sent."""
    open_ids = await uow.session.scalars(
        select(Escalation.id).where(
            Escalation.world_id == uow.world_id,
            Escalation.thread_id == thread.id,
            Escalation.status == "open",
        )
    )
    for escalation_id in list(open_ids):
        await escalations.cancel(uow, escalation_id, reason=f"thread {thread.state}")


async def _queue_closed_notice(uow: UnitOfWork, thread: Thread, closed_by: Side) -> None:
    """The `closed` notice for the other side's last turn (the text `close` queues)."""
    closer = await repo.get_required(
        uow.session, Org, uow.world_id, threads.org_of(thread, closed_by)
    )
    await context.queue_notice(
        uow,
        thread_id=thread.id,
        side=threads.other(closed_by),
        kind="closed",
        text=context.closed_notice(closer.name),
    )


# --- Errors --------------------------------------------------------------------------


@asynccontextmanager
async def domain_errors() -> AsyncGenerator[None]:
    """Domain errors as non-retryable `ApplicationError`s typed by class name."""
    try:
        yield
    except DOMAIN_ERRORS as error:
        raise ApplicationError(
            str(error), _error_details(error), type=type(error).__name__, non_retryable=True
        ) from error


def _error_details(error: Exception) -> dict[str, object]:
    match error:
        case EscalationNotOpen():
            return {
                "status": error.status,
                "resolved_by_member_id": str(error.resolved_by_member_id)
                if error.resolved_by_member_id
                else None,
                "resolved_at": error.resolved_at.isoformat() if error.resolved_at else None,
            }
        case CardNotPending():
            return {"status": error.status}
        case _:
            return {}


def _not_open(escalation: Escalation) -> EscalationNotOpen:
    return EscalationNotOpen(
        escalation.id,
        escalation.status,
        resolved_by_member_id=escalation.resolved_by_member_id,
        resolved_at=escalation.resolved_at,
    )


async def _member_name(session: AsyncSession, world_id: UUID, member_id: UUID) -> str:
    return (await repo.get_required(session, Member, world_id, member_id)).name


async def _org_names(session: AsyncSession, world_id: UUID, org_ids: Sequence[UUID]) -> list[str]:
    if not org_ids:
        return []
    rows = await repo.list_in_world(session, Org, world_id, Org.id.in_(list(org_ids)))
    names = {org.id: org.name for org in rows}
    return [names[org_id] for org_id in org_ids if org_id in names]


__all__ = [
    "DOMAIN_ERRORS",
    "REQUEST_LIFETIME",
    "ApplyResolutionInput",
    "BeginReopenInput",
    "CardResolutionApplied",
    "CardResolutionInput",
    "EndRoundInput",
    "FinishRunInput",
    "NetworkActivities",
    "NotifyConversationInput",
    "NudgeChatInput",
    "OpenThreadInput",
    "ReassignInput",
    "RecordReopenInput",
    "RecordSteerInput",
    "ResolutionApplied",
    "RoundEnded",
    "SettleSteerInput",
    "SettledCardInput",
    "StartRequestInput",
    "SteerRecorded",
    "ThreadStateInput",
    "UnsummarizedInput",
    "conversation_input",
    "domain_errors",
    "nudge_request_chat",
    "run_query",
    "run_update",
    "start_request_workflow",
    "workflow_missing",
]
