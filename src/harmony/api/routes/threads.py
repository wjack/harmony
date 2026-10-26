"""Threads routes, mounted under `/api` by `create_app` (ARCHITECTURE.md §10).

- GET /api/threads: the acting member's inbox, every thread they can see
- GET /api/threads/{thread_id}: one thread; 404, never 403, without access
- POST /api/threads/{thread_id}/share {member_id, question?}: give one teammate access

Every query goes through `auth.access`, so switching accounts changes what these
return. List and snapshot responses carry `X-Harmony-Cursor`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from harmony.api.deps import SessionmakerDep, documented, read_cursor, set_cursor, snapshot_session
from harmony.auth.access import access_reasons, get_visible_thread, visible_to
from harmony.auth.session import Actor, ActorDep
from harmony.auth.views import OrgRef, org_ref
from harmony.db import repo
from harmony.db.models import Member, NetworkRequest, Org, Thread, ThreadAccess, ThreadMessage
from harmony.db.models.choices import (
    AccessReason,
    Provenance,
    Side,
    ThreadMessageKind,
    ThreadState,
)
from harmony.db.uow import unit_of_work
from harmony.domain import threads
from harmony.errors import WorldRetired

router = APIRouter(tags=["threads"])

_NOT_FOUND = "thread not found"


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class ThreadSummary(_View):
    """One inbox row."""

    id: UUID
    # The request's title as the member's side names it (`threads.request_title`).
    title: str
    state: ThreadState
    # The acting member's side of the thread.
    side: Side
    requester: OrgRef
    responder: OrgRef
    # Why the acting member sees it; `access_reason` is the first of `access_reasons`.
    access_reason: AccessReason
    access_reasons: list[AccessReason]
    last_activity_at: datetime
    version: int


class ThreadMessageView(_View):
    id: UUID
    seq: int
    from_org_id: UUID
    kind: ThreadMessageKind
    body: str
    provenance: Provenance
    created_at: datetime


class ThreadAccessView(_View):
    """A member of the acting member's org with a `thread_access` row."""

    member_id: UUID
    member_slug: str
    member_name: str
    reason: AccessReason
    shared_by_member_id: UUID | None
    question: str | None


class ThreadDetail(ThreadSummary):
    side_to_move: Side | None
    deadline_at: datetime | None
    created_at: datetime
    # What crossed the network, in thread order (both parties see these).
    messages: list[ThreadMessageView]
    # Own org only: the other party's access list is theirs.
    access: list[ThreadAccessView]


class ShareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_id: UUID
    # The @mention question that comes with the share (§10).
    question: str | None = Field(default=None, max_length=2000)


class ShareResult(_View):
    thread_id: UUID
    member_id: UUID
    reason: Literal["shared"]
    # False when the member already had this thread shared with them.
    created: bool


@router.get("/threads", responses=documented(401, cursor=True))
async def list_threads(
    actor: ActorDep, sessionmaker: SessionmakerDep, response: Response
) -> list[ThreadSummary]:
    async with snapshot_session(sessionmaker) as db:
        cursor = await read_cursor(db, actor.world_id)
        rows = await _summaries(db, actor)
    set_cursor(response, cursor)
    return [summary for _, summary in rows]


@router.get("/threads/{thread_id}", responses=documented(401, 404, cursor=True))
async def get_thread(
    thread_id: UUID, actor: ActorDep, sessionmaker: SessionmakerDep, response: Response
) -> ThreadDetail:
    async with snapshot_session(sessionmaker) as db:
        cursor = await read_cursor(db, actor.world_id)
        rows = await _summaries(db, actor, Thread.id == thread_id)
        if not rows:
            raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
        [(thread, summary)] = rows
        messages = await db.scalars(
            repo.in_world(ThreadMessage, actor.world_id)
            .where(ThreadMessage.thread_id == thread_id)
            .order_by(ThreadMessage.seq)
        )
        access = await db.execute(
            select(ThreadAccess, Member)
            .join(
                Member,
                and_(
                    Member.world_id == ThreadAccess.world_id,
                    Member.id == ThreadAccess.member_id,
                ),
            )
            .where(
                ThreadAccess.world_id == actor.world_id,
                ThreadAccess.thread_id == thread_id,
                Member.org_id == actor.org_id,
            )
            .order_by(Member.name, ThreadAccess.reason)
        )
        detail = ThreadDetail(
            **summary.model_dump(),
            side_to_move=thread.side_to_move,
            deadline_at=thread.deadline_at,
            created_at=thread.created_at,
            messages=[
                ThreadMessageView(
                    id=message.id,
                    seq=message.seq,
                    from_org_id=message.from_org_id,
                    kind=message.kind,
                    body=message.body,
                    provenance=message.provenance,
                    created_at=message.created_at,
                )
                for message in messages
            ],
            access=[
                ThreadAccessView(
                    member_id=member.id,
                    member_slug=member.slug,
                    member_name=member.name,
                    reason=row.reason,
                    shared_by_member_id=row.shared_by_member_id,
                    question=row.question,
                )
                for row, member in access
            ],
        )
    set_cursor(response, cursor)
    return detail


@router.post("/threads/{thread_id}/share", responses=documented(400, 401, 403, 404, 409))
async def share_thread(
    thread_id: UUID, body: ShareRequest, actor: ActorDep, sessionmaker: SessionmakerDep
) -> ShareResult:
    """Give exactly one member of your own org access to a thread you can see.

    Emits `thread.access_changed` to that member. Sharing again is a no-op.
    """
    if body.member_id == actor.member_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "you already have access")
    try:
        async with unit_of_work(sessionmaker, actor.world_id) as uow:
            session = uow.session
            try:
                thread = await get_visible_thread(session, actor, thread_id)
            except repo.NotFoundInWorld:
                raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from None
            member = await repo.get(session, Member, actor.world_id, body.member_id)
            if member is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
            if member.org_id != actor.org_id:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, "threads can be shared only within your org"
                )
            created = await threads.add_access(
                uow,
                thread,
                member_id=member.id,
                org_id=member.org_id,
                reason="shared",
                shared_by_member_id=actor.member_id,
                question=body.question,
            )
            await uow.commit()
    except WorldRetired:
        raise HTTPException(status.HTTP_409_CONFLICT, "this world was retired") from None
    return ShareResult(thread_id=thread_id, member_id=member.id, reason="shared", created=created)


async def _summaries(
    db: AsyncSession, actor: Actor, *where: ColumnElement[bool]
) -> Sequence[tuple[Thread, ThreadSummary]]:
    """The threads the actor can see (matching `where`), newest activity first."""
    requester, responder = aliased(Org), aliased(Org)
    rows = (
        await db.execute(
            select(Thread, NetworkRequest, requester, responder)
            .join(
                NetworkRequest,
                and_(
                    NetworkRequest.world_id == Thread.world_id,
                    NetworkRequest.id == Thread.request_id,
                ),
            )
            .join(
                requester,
                and_(
                    requester.world_id == Thread.world_id,
                    requester.id == Thread.requester_org_id,
                ),
            )
            .join(
                responder,
                and_(
                    responder.world_id == Thread.world_id,
                    responder.id == Thread.responder_org_id,
                ),
            )
            .where(visible_to(actor), *where)
            # `id` only breaks ties deterministically; it carries no order of its own.
            .order_by(Thread.last_activity_at.desc(), Thread.id)
        )
    ).all()
    reasons = await access_reasons(db, actor, [row[0] for row in rows])
    openings = await threads.opening_lines(
        db, actor.world_id, [row[0].id for row in rows if row[0].requester_org_id != actor.org_id]
    )
    return [
        (
            thread,
            ThreadSummary(
                id=thread.id,
                title=threads.request_title(
                    actor.org_id,
                    request,
                    requester=requester_org.name,
                    opening=openings.get(thread.id),
                ),
                state=thread.state,
                side="requester" if thread.requester_org_id == actor.org_id else "responder",
                requester=org_ref(requester_org),
                responder=org_ref(responder_org),
                access_reason=reasons[thread.id][0],
                access_reasons=reasons[thread.id],
                last_activity_at=thread.last_activity_at,
                version=thread.version,
            ),
        )
        for thread, request, requester_org, responder_org in rows
    ]
