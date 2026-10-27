"""Threads routes, mounted under `/api` by `create_app` (ARCHITECTURE.md §10).

- GET /api/threads: the acting member's inbox, every thread they can see
- GET /api/threads/{thread_id}: one thread; 404, never 403, without access
- POST /api/threads/{thread_id}/share {member_id, question?}: give one teammate access
- POST /api/threads/{thread_id}/reopen {client_key, as_member_id, body}: start another
  run of a finished thread (docs/design/REOPEN.md §7.2); answers `ApiProblem`s

Every query goes through `auth.access`, so switching accounts changes what these
return. List and snapshot responses carry `X-Harmony-Cursor`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from harmony.agents.models import MemberRef
from harmony.api.deps import (
    ServicesDep,
    SessionmakerDep,
    documented,
    read_cursor,
    set_cursor,
    snapshot_session,
)
from harmony.api.routes.problems import member_ref, problems, temporal_of
from harmony.auth.access import access_reasons, get_visible_thread, visible_to
from harmony.auth.session import Actor, ActorDep
from harmony.auth.views import OrgRef, org_ref
from harmony.db import repo
from harmony.db.models import (
    Member,
    NetworkRequest,
    Org,
    Thread,
    ThreadAccess,
    ThreadMessage,
    ThreadRun,
)
from harmony.db.models.choices import (
    AccessReason,
    EndReason,
    FailureReason,
    Provenance,
    ReopenBlock,
    Side,
    ThreadMessageKind,
    ThreadState,
)
from harmony.db.uow import unit_of_work
from harmony.domain import reopens, threads
from harmony.errors import WorldRetired
from harmony.temporal.reopen import start_reopen
from harmony.temporal.workflows.payloads import STEER_BODY_MAX_CHARS

router = APIRouter(tags=["threads"])

_NOT_FOUND = "thread not found"


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class ThreadSummary(_View):
    """One inbox row."""

    id: UUID
    # As the member's side names the thread (`threads.thread_title`).
    title: str
    state: ThreadState
    # Why it stopped early (a cap, the pause, a model error); None otherwise.
    stop_reason: FailureReason | None = None
    # The acting member's side of the thread.
    side: Side
    requester: OrgRef
    responder: OrgRef
    # Why the acting member sees it; `access_reason` is the first of `access_reasons`.
    access_reason: AccessReason
    access_reasons: list[AccessReason]
    last_activity_at: datetime
    # The current run: 1, plus one per Reopen.
    run: int
    # How the current run ended; None while it runs, and on rows ended before Reopen.
    end_reason: EndReason | None
    ended_at: datetime | None
    # The side whose representative closed it, when `end_reason` is `closed`.
    closed_by: Side | None
    # When the run's workflows finished their wrap-up; Reopen waits for it.
    run_finished_at: datetime | None
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


class ReopenOffer(_View):
    """Whether the acting member's side may reopen the thread now."""

    state: Literal["allowed", "wrapping_up", "blocked"]
    # Why not, when `blocked`.
    block: ReopenBlock | None


class RunReopened(_View):
    """Who started a run after the first: the org, and the member and steer only for
    the viewer's own side."""

    org: OrgRef
    side: Side
    by: MemberRef | None
    steer_id: UUID | None


class RunEnded(_View):
    state: ThreadState
    end_reason: EndReason
    closed_by: Side | None
    stop_reason: FailureReason | None
    at: datetime


class ThreadRunView(_View):
    run: int
    started_at: datetime
    # None for run 1.
    reopened: RunReopened | None
    # None while the run runs.
    ended: RunEnded | None


class ThreadDetail(ThreadSummary):
    side_to_move: Side | None
    deadline_at: datetime | None
    created_at: datetime
    # What crossed the network, in thread order (both parties see these).
    messages: list[ThreadMessageView]
    # Own org only: the other party's access list is theirs.
    access: list[ThreadAccessView]
    # None while the thread is open.
    reopen: ReopenOffer | None
    # Every run that started, oldest first.
    runs: list[ThreadRunView]


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


class ReopenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Makes a retry the same Reopen (the steer's id is minted from it).
    client_key: UUID
    # The member the tab shows; 409 `member_changed` when the session has switched.
    as_member_id: UUID
    # What the member tells its side's Claude to do with the reopened thread.
    body: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=STEER_BODY_MAX_CHARS),
    ]


class ReopenStarted(_View):
    steer_id: UUID
    thread_id: UUID


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
        side = summary.side
        runs = await threads.runs(db, actor.world_id, thread_id)
        reopen_offer = reopens.reopen_offer(
            thread,
            runs,
            side,
            requester_wrote=await reopens.requester_wrote(db, thread),
            now=datetime.now(UTC),
        )
        detail = ThreadDetail(
            **summary.model_dump(),
            reopen=(
                ReopenOffer(state=reopen_offer.state, block=reopen_offer.block)
                if reopen_offer
                else None
            ),
            runs=await _run_views(db, actor, thread, runs),
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


@router.post(
    "/threads/{thread_id}/reopen",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ReopenStarted,
    responses=problems(
        status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT
    ),
)
async def reopen_thread(
    thread_id: UUID, body: ReopenRequest, actor: ActorDep, services: ServicesDep
) -> ReopenStarted:
    """Start another run of a finished thread from the acting member's side.

    409 `member_changed` (with `member`) first, then 404, 403 `not_in_org`, 409
    `thread_open` (with `reopened_by`), `reopen_in_progress`, `closing_in_progress`,
    `not_reopenable` (with `block`) and `world_retired`.
    """
    steer_id = await start_reopen(
        temporal_of(services),
        services.sessionmaker,
        actor,
        thread_id,
        client_key=body.client_key,
        as_member_id=body.as_member_id,
        body=body.body,
    )
    return ReopenStarted(steer_id=steer_id, thread_id=thread_id)


async def _run_views(
    db: AsyncSession, actor: Actor, thread: Thread, runs: Sequence[ThreadRun]
) -> list[ThreadRunView]:
    """Every run that started (REOPEN.md §7.3). Who reopened a run, and with which
    steer, only for the viewer's own org; the current run's end from the thread row,
    which has it before `finish_run` copies it. A seeded thread has no runs: its one
    run is the thread itself."""
    if not runs:
        return [
            ThreadRunView(
                run=thread.run, started_at=thread.created_at, reopened=None, ended=_ended(thread)
            )
        ]
    org_ids = {row.reopened_by_org_id for row in runs if row.reopened_by_org_id is not None}
    orgs = {
        org.id: org
        for org in await repo.list_in_world(db, Org, actor.world_id, Org.id.in_(org_ids))
    }
    views: list[ThreadRunView] = []
    for row in runs:
        reopened = None
        if row.reopened_by_org_id is not None and row.reopened_side is not None:
            own = row.reopened_by_org_id == actor.org_id
            by = (
                await member_ref(db, actor.world_id, row.reopened_by_member_id)
                if own and row.reopened_by_member_id is not None
                else None
            )
            reopened = RunReopened(
                org=org_ref(orgs[row.reopened_by_org_id]),
                side=row.reopened_side,
                by=by,
                steer_id=row.steer_id if own else None,
            )
        views.append(
            ThreadRunView(
                run=row.run,
                started_at=row.started_at,
                reopened=reopened,
                ended=_ended(thread) if row.run == thread.run else _run_ended(row),
            )
        )
    return views


def _ended(thread: Thread) -> RunEnded | None:
    if thread.state not in threads.TERMINAL_STATES or thread.ended_at is None:
        return None
    return RunEnded(
        state=thread.state,
        # A seeded thread's rows carry it; nothing else ends without one.
        end_reason=thread.end_reason or "closed",
        closed_by=thread.closed_by,
        stop_reason=thread.stop_reason,
        at=thread.ended_at,
    )


def _run_ended(row: ThreadRun) -> RunEnded | None:
    if row.state is None or row.end_reason is None or row.ended_at is None:
        return None
    return RunEnded(
        state=row.state,
        end_reason=row.end_reason,
        closed_by=row.closed_by,
        stop_reason=row.stop_reason,
        at=row.ended_at,
    )


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
                title=threads.thread_title(
                    thread,
                    request,
                    actor.org_id,
                    requester=requester_org.name,
                    opening=openings.get(thread.id),
                ),
                state=thread.state,
                stop_reason=thread.stop_reason,
                side="requester" if thread.requester_org_id == actor.org_id else "responder",
                requester=org_ref(requester_org),
                responder=org_ref(responder_org),
                access_reason=reasons[thread.id][0],
                access_reasons=reasons[thread.id],
                last_activity_at=thread.last_activity_at,
                run=thread.run,
                end_reason=thread.end_reason,
                ended_at=thread.ended_at,
                closed_by=thread.closed_by,
                run_finished_at=thread.run_finished_at,
                version=thread.version,
            ),
        )
        for thread, request, requester_org, responder_org in rows
    ]
