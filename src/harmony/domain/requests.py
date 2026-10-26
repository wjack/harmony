"""Network requests: an approved brief and its recipients (§4, §7).

`create_from_card` turns an approved outreach card into a `network_requests` row (the
brief as approved, the approved recipients with fit and reason, `card_id`, the origin
conversation). The `NetworkRequestWorkflow` (`w:{world}:req:{id}`) then opens one
thread per recipient.

`status` is the per-recipient view the request status card and `GET /api/requests/{id}`
show: each recipient's participant status, its thread, who it waits on, its latest
line, and the requester side's close summary. The orgs the outreach card proposed but
the member left out follow the approved ones, as `screened` for good (§10).

Participant status (REVISION_UNSTRUCTURED.md §9), from thread and escalation state:
- `screened`: its thread hasn't opened yet, or it was screened and left out;
- `messaged`: the thread is open and the responder hasn't written yet;
- `replied`: the responder has written;
- `needs_human`: an escalation on the thread is open (either side);
- `closed`: the thread ended (`closed` or `expired`).

`waiting_on` names one org's assignee only (the requester's by default): who holds up
the other side is theirs to know. The network view asks for the viewer's own org, or
for any org on the network lens (`waiting_of`).

Writes queue a `network_request` snapshot to the origin conversation's owner and to the
requester org's scope on each of the request's threads.

Owner: E4.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, TypeAdapter
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents.models import (
    Brief,
    CardResolution,
    MemberRef,
    OutreachRecipient,
    ParticipantStatus,
    RequestStatus,
    ScreenedCounts,
    brief_title,
    first_line,
)
from harmony.auth.access import Viewer, visible_threads
from harmony.auth.views import OrgRef, org_ref
from harmony.db import repo
from harmony.db.models import (
    Card,
    Conversation,
    Escalation,
    Member,
    NetworkRequest,
    Org,
    RepSession,
    Thread,
    ThreadMessage,
)
from harmony.db.models.choices import Fit, ThreadState
from harmony.db.uow import UnitOfWork
from harmony.domain import cards
from harmony.domain.threads import TERMINAL_STATES
from harmony.events.schemas import EventScope

# A recipient's latest line on the request status card.
LATEST_LINE_CHARS = 160
_RECIPIENTS: TypeAdapter[list[OutreachRecipient]] = TypeAdapter(list[OutreachRecipient])


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class WaitingOn(_View):
    """Who a recipient's thread waits on, and since when ("needs a human")."""

    member: MemberRef
    since: datetime


class RecipientStatus(_View):
    org: OrgRef
    fit: Fit | None
    reason: str
    participant: ParticipantStatus
    thread_id: UUID | None
    thread_state: ThreadState | None
    waiting_on: WaitingOn | None
    # The thread's newest message (either side) after its sender's name, cut to one
    # line; None before any.
    latest_line: str | None
    # The requester side's own close summary, once its representative wrote one.
    close_summary: str | None
    last_activity_at: datetime | None


class RequestStatusView(_View):
    """`GET /api/requests/{id}` and the chat's live request status card."""

    id: UUID
    # `agents.models.brief_title(brief)`.
    title: str
    status: RequestStatus
    brief: Brief
    conversation_id: UUID | None
    created_at: datetime
    # From the outreach card the request was approved from.
    screened: ScreenedCounts | None
    recipients: list[RecipientStatus]
    version: int


async def create_from_card(
    uow: UnitOfWork, card: Card, resolution: CardResolution, *, request_id: UUID
) -> NetworkRequest:
    """The request an approved outreach card describes, with the member's edits
    applied (recipients narrowed to the approved subset). Idempotent on `request_id`
    and on the card."""
    session = uow.session
    existing = await repo.get(session, NetworkRequest, uow.world_id, request_id)
    if existing is None:
        existing = await session.scalar(
            repo.in_world(NetworkRequest, uow.world_id).where(NetworkRequest.card_id == card.id)
        )
    if existing is not None:
        return existing
    payload = cards.payload_of(card)
    edits = resolution.edits
    brief = edits.brief if edits is not None and edits.brief is not None else payload.brief
    chosen = edits.recipients if edits and edits.recipients is not None else None
    recipients = [r for r in payload.recipients if chosen is None or r.org_id in chosen]
    if not recipients:
        raise ValueError("an outreach approval needs at least one recipient")
    conversation = await repo.get_required(
        session, Conversation, uow.world_id, card.conversation_id
    )
    request = NetworkRequest(
        world_id=uow.world_id,
        id=request_id,
        requester_org_id=conversation.org_id,
        conversation_id=conversation.id,
        created_by_member_id=resolution.member_id,
        card_id=card.id,
        brief=brief,
        recipients=[r.model_dump(mode="json") for r in recipients],
        status="active",
    )
    session.add(request)
    await session.flush()
    await _queue(uow, request)
    return request


async def set_status(uow: UnitOfWork, request_id: UUID, status: RequestStatus) -> NetworkRequest:
    """Move the request to `status`; `closed` is final."""
    request = await uow.session.get(
        NetworkRequest, (uow.world_id, request_id), with_for_update=True, populate_existing=True
    )
    if request is None:
        raise repo.NotFoundInWorld(NetworkRequest, uow.world_id, request_id)
    if request.status in (status, "closed"):
        return request
    request.status = status
    await uow.session.flush()
    await _queue(uow, request)
    return request


def recipients_of(request: NetworkRequest) -> list[OutreachRecipient]:
    return _RECIPIENTS.validate_python(request.recipients)


async def threads_of(session: AsyncSession, request: NetworkRequest) -> list[Thread]:
    """The request's threads, oldest first."""
    return list(
        await session.scalars(
            repo.in_world(Thread, request.world_id)
            .where(Thread.request_id == request.id)
            .order_by(Thread.created_at, Thread.id)
        )
    )


async def status(
    session: AsyncSession,
    request: NetworkRequest,
    *,
    waiting_of: UUID | Literal["any"] | None = None,
) -> RequestStatusView:
    """The per-recipient view (module docstring). `waiting_of`: whose assignees
    `waiting_on` may name: that org's, any org's, or (None) the requester's."""
    world_id = request.world_id
    waiting_org = request.requester_org_id if waiting_of is None else waiting_of
    threads = {thread.responder_org_id: thread for thread in await threads_of(session, request)}
    card = await repo.get(session, Card, world_id, request.card_id) if request.card_id else None
    recipients = recipients_of(request)
    listed = {recipient.org_id for recipient in recipients}
    recipients += [
        OutreachRecipient(org_id=org_id, fit=None, reason="")
        for org_id in threads
        if org_id not in listed
    ]
    if card is not None:
        listed |= threads.keys()
        recipients += [r for r in cards.payload_of(card).recipients if r.org_id not in listed]
    org_ids = {recipient.org_id for recipient in recipients}
    orgs = {
        org.id: org_ref(org)
        for org in await repo.list_in_world(session, Org, world_id, Org.id.in_(org_ids))
    }
    thread_ids = [thread.id for thread in threads.values()]
    replied = set(
        await session.scalars(
            select(ThreadMessage.thread_id)
            .join(
                Thread,
                (Thread.world_id == ThreadMessage.world_id)
                & (Thread.id == ThreadMessage.thread_id),
            )
            .where(
                ThreadMessage.world_id == world_id,
                ThreadMessage.thread_id.in_(thread_ids),
                ThreadMessage.from_org_id == Thread.responder_org_id,
            )
            .distinct()
        )
    )
    latest = await _latest_lines(session, world_id, thread_ids)
    summaries = await _requester_summaries(session, world_id, thread_ids)
    open_escalations = list(
        await session.scalars(
            repo.in_world(Escalation, world_id)
            .where(Escalation.thread_id.in_(thread_ids), Escalation.status == "open")
            .order_by(Escalation.created_at, Escalation.id)
        )
    )
    members = await _member_refs(session, world_id, open_escalations)

    rows: list[RecipientStatus] = []
    for recipient in recipients:
        org = orgs.get(recipient.org_id)
        if org is None:
            continue
        thread = threads.get(recipient.org_id)
        if thread is None:
            rows.append(
                RecipientStatus(
                    org=org,
                    fit=recipient.fit,
                    reason=recipient.reason,
                    participant="screened",
                    thread_id=None,
                    thread_state=None,
                    waiting_on=None,
                    latest_line=None,
                    close_summary=None,
                    last_activity_at=None,
                )
            )
            continue
        escalations = [e for e in open_escalations if e.thread_id == thread.id]
        rows.append(
            RecipientStatus(
                org=org,
                fit=recipient.fit,
                reason=recipient.reason,
                participant=participant(
                    thread, needs_human=bool(escalations), replied=thread.id in replied
                ),
                thread_id=thread.id,
                thread_state=thread.state,
                waiting_on=_waiting_on(
                    escalations,
                    members,
                    org_id=None if waiting_org == "any" else waiting_org,
                ),
                latest_line=latest.get(thread.id),
                close_summary=summaries.get(thread.id),
                last_activity_at=thread.last_activity_at,
            )
        )
    return RequestStatusView(
        id=request.id,
        title=brief_title(request.brief),
        status=request_status(request.status),
        brief=request.brief,
        conversation_id=request.conversation_id,
        created_at=request.created_at,
        screened=cards.payload_of(card).screened if card is not None else None,
        recipients=rows,
        version=request.version,
    )


async def readable_threads(
    session: AsyncSession, viewer: Viewer, request: NetworkRequest
) -> set[UUID] | None:
    """The request's threads `viewer` (a member of the requester org) can open; None for
    the origin chat's owner, whose status card reports on all of them. Empty when they
    may not see the request at all."""
    if request.conversation_id is not None:
        conversation = await repo.get(
            session, Conversation, viewer.world_id, request.conversation_id
        )
        if conversation is not None and conversation.owner_member_id == viewer.member_id:
            return None
    return set(
        await session.scalars(
            visible_threads(viewer)
            .where(Thread.request_id == request.id)
            .with_only_columns(Thread.id)
        )
    )


def scoped(view: RequestStatusView, readable: set[UUID] | None) -> RequestStatusView:
    """`view` as a member other than the origin chat's owner may read it
    (`readable_threads`): what a thread says (its latest line, the requester's close
    summary, who it waits on) blanked on the threads outside `readable`, those they
    can't open, and no origin chat, which is its owner's alone. None reads it all."""
    if readable is None:
        return view
    hidden = {"latest_line": None, "close_summary": None, "waiting_on": None}
    recipients = [
        row if row.thread_id is None or row.thread_id in readable else row.model_copy(update=hidden)
        for row in view.recipients
    ]
    return view.model_copy(update={"recipients": recipients, "conversation_id": None})


def participant(thread: Thread, *, needs_human: bool, replied: bool) -> ParticipantStatus:
    """A thread's participant status (module docstring), an ended thread first."""
    if thread.state in TERMINAL_STATES:
        return "closed"
    if needs_human:
        return "needs_human"
    return "replied" if replied else "messaged"


def request_status(value: str) -> RequestStatus:
    match value:
        case "active" | "closed":
            return value
        case _:
            return "active"


async def _latest_lines(
    session: AsyncSession, world_id: UUID, thread_ids: Sequence[UUID]
) -> dict[UUID, str]:
    """Each thread's newest message, as one line after its sender's name: either side
    may have written it."""
    rows = await session.execute(
        select(ThreadMessage.thread_id, Org.name, ThreadMessage.body)
        .join(Org, (Org.world_id == ThreadMessage.world_id) & (Org.id == ThreadMessage.from_org_id))
        .where(
            ThreadMessage.world_id == world_id,
            ThreadMessage.thread_id.in_(thread_ids),
            ThreadMessage.kind == "message",
        )
        .ext(distinct_on(ThreadMessage.thread_id))
        .order_by(ThreadMessage.thread_id, ThreadMessage.seq.desc())
    )
    return {
        thread_id: f"{name}: {first_line(body, LATEST_LINE_CHARS)}"
        for thread_id, name, body in rows.all()
    }


async def _requester_summaries(
    session: AsyncSession, world_id: UUID, thread_ids: Sequence[UUID]
) -> dict[UUID, str]:
    rows = await session.execute(
        select(RepSession.thread_id, RepSession.close_summary).where(
            RepSession.world_id == world_id,
            RepSession.thread_id.in_(thread_ids),
            RepSession.side == "requester",
            RepSession.close_summary.is_not(None),
        )
    )
    return {thread_id: summary for thread_id, summary in rows.all() if summary is not None}


async def _member_refs(
    session: AsyncSession, world_id: UUID, escalations: Sequence[Escalation]
) -> dict[UUID, MemberRef]:
    ids = {UUID(str(member_id)) for e in escalations for member_id in e.assignees}
    if not ids:
        return {}
    return {
        member.id: MemberRef(id=member.id, slug=member.slug, name=member.name)
        for member in await repo.list_in_world(session, Member, world_id, Member.id.in_(ids))
    }


def _waiting_on(
    escalations: Sequence[Escalation], members: dict[UUID, MemberRef], *, org_id: UUID | None
) -> WaitingOn | None:
    for escalation in escalations:
        if org_id is not None and escalation.org_id != org_id:
            continue
        for member_id in escalation.assignees:
            member = members.get(UUID(str(member_id)))
            if member is not None:
                return WaitingOn(member=member, since=escalation.created_at)
    return None


async def _queue(uow: UnitOfWork, request: NetworkRequest) -> None:
    if request.conversation_id is not None:
        conversation = await repo.get(
            uow.session, Conversation, uow.world_id, request.conversation_id
        )
        if conversation is not None:
            uow.add_snapshot(
                request, EventScope.member(conversation.owner_member_id, conversation.org_id)
            )
    thread_ids = await uow.session.scalars(
        select(Thread.id).where(Thread.world_id == uow.world_id, Thread.request_id == request.id)
    )
    for thread_id in thread_ids:
        uow.add_snapshot(request, EventScope.thread(request.requester_org_id, thread_id))


__all__ = [
    "RecipientStatus",
    "RequestStatusView",
    "WaitingOn",
    "create_from_card",
    "participant",
    "readable_threads",
    "recipients_of",
    "request_status",
    "scoped",
    "set_status",
    "status",
    "threads_of",
]
