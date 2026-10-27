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
from datetime import timedelta
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
from harmony.agents.models import CardResolution, EscalationResolution, RequestStatus
from harmony.agents.runtime import OwedResult
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
from harmony.db.models.choices import FailureReason, Side, ThreadState
from harmony.db.uow import UnitOfWork, unit_of_work
from harmony.domain import cards, consults, conversations, escalations, notes, requests, threads
from harmony.domain.errors import (
    CardNotPending,
    ConsultNotOpen,
    EscalationNotOpen,
    InvalidRoute,
    NotAdmin,
    NotInOrg,
)
from harmony.temporal.policies import ensure_world_live
from harmony.temporal.start import start_world_workflow
from harmony.temporal.workflows.payloads import (
    CardResolved,
    ConversationCarry,
    ConversationInput,
    EscalationResolved,
    NetworkRequestInput,
    NetworkUpdate,
    Reassigned,
    Reassignment,
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


class RedraftFailedInput(_Payload):
    world_id: UUID
    escalation_id: UUID
    draft_rev: int


class ThreadStateInput(_Payload):
    world_id: UUID
    thread_id: UUID
    state: ThreadState
    side_to_move: Side | None = None
    # The side that ended the thread (a refusal): when this call ends it, the other
    # side is queued the `closed` notice for its last turn. A `close` queues its own.
    closed_by: Side | None = None
    # Why the system ended it, when it did (3.5): stored with the terminal state.
    stop_reason: FailureReason | None = None


class UnsummarizedInput(_Payload):
    world_id: UUID
    thread_id: UUID
    org_id: UUID
    # The last turn that failed: it names the note, so a retry writes it once.
    turn_id: UUID
    reason: FailureReason


class RequestStatusInput(_Payload):
    world_id: UUID
    request_id: UUID
    status: RequestStatus


class NotifyConversationInput(_Payload):
    world_id: UUID
    conversation_id: UUID
    update: NetworkUpdate


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
    async def report_redraft_failed(self, failed: RedraftFailedInput) -> None:
        """`escalations.redraft_failed`. A retry after the commit says it again, which
        the panel takes the same way."""
        await ensure_world_live(self._sessionmaker, failed.world_id)
        async with domain_errors(), unit_of_work(self._sessionmaker, failed.world_id) as uow:
            await escalations.redraft_failed(uow, failed.escalation_id, failed.draft_rev)
            await uow.commit()

    @activity.defn
    async def set_thread_state(self, state: ThreadStateInput) -> None:
        """Move the thread. An ended thread's open escalations are cancelled, and when
        this call is what ends it, `closed_by`'s counterparty is queued the `closed`
        notice for its last turn."""
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
                stop_reason=state.stop_reason,
            )
            if thread.state in threads.TERMINAL_STATES:
                await _cancel_open_items(uow, thread)
            if ending and state.closed_by is not None:
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
    async def set_request_status(self, status: RequestStatusInput) -> None:
        await ensure_world_live(self._sessionmaker, status.world_id)
        async with domain_errors(), unit_of_work(self._sessionmaker, status.world_id) as uow:
            await requests.set_status(uow, status.request_id, status.status)
            await uow.commit()

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


# --- Shared with the API -------------------------------------------------------------


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
    turns it has already taken, so cassette turn numbers continue after an idle exit."""
    owner = await repo.get_required(
        session, Member, conversation.world_id, conversation.owner_member_id
    )
    return ConversationInput(
        world_id=conversation.world_id,
        conversation_id=conversation.id,
        org_id=conversation.org_id,
        owner_member_id=conversation.owner_member_id,
        cassette_thread=chat_key(owner.slug, await conversations.ordinal(session, conversation)),
        carried=ConversationCarry(turns=await conversations.turns_taken(session, conversation)),
    )


async def run_update[R](
    handle: WorkflowHandle[Any, Any], update: str, arg: BaseModel, result_type: type[R]
) -> R:
    """`execute_update` by name with a typed result (the SDK's overloads leave the
    by-name form partly untyped)."""
    result: R = await handle.execute_update(  # pyright: ignore[reportUnknownMemberType]
        update, arg, result_type=result_type
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
    nothing went out."""
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
        return CardResolutionApplied(
            resolved=CardResolved(card_id=card.id, status=card.status),
            tool_use_id=card.tool_use_id,
            content=f"rejected: {feedback}" if feedback else "rejected",
        )
    request = await session.scalar(
        repo.in_world(NetworkRequest, card.world_id).where(NetworkRequest.card_id == card.id)
    )
    request_id = request.id if request is not None else request_id
    names = await _org_names(
        session,
        card.world_id,
        [r.org_id for r in requests.recipients_of(request)] if request else [],
    )
    content = (
        f"Sent without review (this chat's setting): request {request_id} to {', '.join(names)}."
        if card.auto
        else f"approved; request {request_id} sent to {', '.join(names)}"
    )
    return CardResolutionApplied(
        resolved=CardResolved(card_id=card.id, status=card.status, request_id=request_id),
        tool_use_id=card.tool_use_id,
        content=content,
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
        case "revise":
            raise ValueError("only a held message can be sent back to be revised")


async def _apply_held(
    uow: UnitOfWork,
    escalation: Escalation,
    thread: Thread,
    applied: ApplyResolutionInput,
    side: Side,
) -> None:
    """A message the representative wrote in *Ask*, held for a person: approved, it goes
    out as written or as the member edited it, under `escalations.held_message_id`,
    provenance `agent_approved`; `take_over` sends the member's own message instead;
    `decline` and `revise` send nothing."""
    resolution = applied.resolution
    text = (resolution.text or "").strip()
    match resolution.action:
        case "approve":
            draft = await escalations.current_draft(uow.session, escalation)
            written = text or (draft.answer if draft is not None else "")
            if not written:
                raise ValueError("an approval needs the message to send")
            await threads.append_message(
                uow,
                thread.id,
                message_id=escalations.held_message_id(escalation),
                from_org_id=escalation.org_id,
                kind="message",
                body=written,
                provenance="agent_approved",
                turn_id=draft.turn_id if draft is not None else None,
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
        case "decline" | "revise":
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
        draft = await escalations.current_draft(session, escalation)
        resolver = await repo.get_required(
            session, Member, escalation.world_id, resolution.member_id
        )
        owed, sent = _held_outcome(resolution, draft, resolver_name=resolver.name)
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
    resolution: EscalationResolution, draft: EscalationDraft | None, *, resolver_name: str
) -> tuple[OwedResult, bool]:
    """A resolved hold's result for the representative, and whether a message reached
    the counterparty."""
    content = answerer.held_result(
        resolution.action,
        text=resolution.text or "",
        drafted=draft.answer if draft is not None else "",
        member_name=resolver_name,
    )
    unsent = resolution.action in ("decline", "revise")
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
    "CardResolutionApplied",
    "CardResolutionInput",
    "NetworkActivities",
    "NotifyConversationInput",
    "OpenThreadInput",
    "ReassignInput",
    "RequestStatusInput",
    "ResolutionApplied",
    "SettledCardInput",
    "StartRequestInput",
    "ThreadStateInput",
    "UnsummarizedInput",
    "conversation_input",
    "domain_errors",
    "run_query",
    "run_update",
    "start_request_workflow",
    "workflow_missing",
]
