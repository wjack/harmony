"""Network route: the network view's graph (§11, UI_REFERENCE §8).

- GET /api/network?request=<id>: orgs as nodes and threads as edges

Read in whichever world `current_actor` resolves (`?world=desk|network`). Without
`request`, the most recent request the acting member can see that is still live or was
sent from a chat is focused: a world's seeded history is there to pick, not to open on.
Nodes are the world's orgs (bystanders included, for the "whole network" look); edges
are the focused request's threads, with each recipient's participant status.

The firewall holds here too (§11.3). The lens and the requester's own members see every
recipient, with the screener's `fit` and, on a request sent from a chat, the story's
`suggested` marks (seeded requests had no card to suggest anyone on). A recipient's
members see only the requester, their own org and their own thread: who else the
requester contacted, how it rated them and how those threads stand are the requester's
business (§11.6). Only threads the acting member can see are detailed (latest line,
turn count, and the acting member's own org's close summary, which never shows to the
other side: REVISION_UNSTRUCTURED.md §5); others appear as bare edges (state only). Carries
`X-Harmony-Cursor`; `thread` and `network_request` snapshots keep it live,
`stream.typing` animates edges.

Owner: E4 (U4 builds the view).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents.models import ParticipantStatus, RequestStatus
from harmony.api.deps import SessionmakerDep, read_cursor, set_cursor, snapshot_session
from harmony.api.routes.demo import scenario_story
from harmony.api.routes.problems import not_found, problems
from harmony.auth import access
from harmony.auth.session import Actor, ActorDep
from harmony.auth.views import OrgRef, org_ref
from harmony.db import repo
from harmony.db.models import Conversation, NetworkRequest, Org, RepSession, Thread, World
from harmony.db.models.choices import FailureReason, Fit, ThreadState
from harmony.domain import requests, threads
from harmony.domain.requests import RecipientStatus, WaitingOn

router = APIRouter(tags=["network"])

# The request picker's length.
REQUESTS_LIMIT: Final = 50


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class NetworkRequestRef(_View):
    id: UUID
    # The requester's side and the lens: the request's title (`threads.requester_title`);
    # a recipient: its own thread's (`threads.thread_title`).
    title: str
    requester: OrgRef
    status: RequestStatus
    created_at: datetime


NodeRole = Literal["requester", "recipient", "bystander"]


class NetworkNode(_View):
    org: OrgRef
    # The org's category (`orgs.kind`): research, characterization, fabrication, ...
    kind: str
    featured: bool
    role: NodeRole
    # For recipients of the focused request.
    participant: ParticipantStatus | None
    fit: Fit | None
    # The recipient's thread waits on a human (shown only to the member's own org, on a
    # thread they can open, or to everyone on the network lens).
    waiting_on: WaitingOn | None
    # One of the story's suggested recipients, on a request sent from a chat.
    suggested: bool


class NetworkEdge(_View):
    thread_id: UUID
    from_org_id: UUID
    to_org_id: UUID
    state: ThreadState
    # Why it stopped early (a cap, the pause, a model error): status, like `state`.
    stop_reason: FailureReason | None = None
    # Detailed only when the acting member can see the thread.
    latest_line: str | None
    turn_count: int | None
    # The acting member's own org's close summary for this thread, once written.
    close_summary: str | None
    last_activity_at: datetime


class NetworkCounts(_View):
    orgs: int
    threads: int
    active: int
    closed: int


class NetworkView(_View):
    world_id: UUID
    requests: list[NetworkRequestRef]
    request_id: UUID | None
    nodes: list[NetworkNode]
    edges: list[NetworkEdge]
    counts: NetworkCounts


@router.get(
    "/network",
    response_model=NetworkView,
    responses=problems(status.HTTP_404_NOT_FOUND, cursor=True),
)
async def get_network(
    actor: ActorDep,
    sessionmaker: SessionmakerDep,
    response: Response,
    request_id: Annotated[UUID | None, Query(alias="request")] = None,
) -> NetworkView:
    async with snapshot_session(sessionmaker) as db:
        view = await _network(db, actor, request_id)
        cursor = await read_cursor(db, actor.world_id)
    set_cursor(response, cursor)
    return view


async def _network(db: AsyncSession, actor: Actor, request_id: UUID | None) -> NetworkView:
    world = await db.get(World, actor.world_id)
    if world is None:
        raise not_found("world")
    orgs = list(
        await db.scalars(repo.in_world(Org, actor.world_id).order_by(Org.created_at, Org.slug))
    )
    refs = {org.id: org_ref(org) for org in orgs}
    listed = await _visible_requests(db, actor)
    focused = _focus(listed, request_id)
    recipients: dict[UUID, RecipientStatus] = {}
    edges: list[NetworkEdge] = []
    full = focused is not None and _sees_all_recipients(actor, focused)
    if focused is not None:
        view = await requests.status(
            db, focused, waiting_of="any" if actor.lens == "network" else actor.org_id
        )
        shown = await _shown_threads(db, actor, focused, full=full)
        seen = await _seen(db, actor, shown)
        view = requests.scoped(view, None if actor.lens == "network" else seen)
        rows = [row for row in view.recipients if full or row.org.id == actor.org_id]
        recipients = {row.org.id: row for row in rows}
        edges = await _edges(db, actor, shown, seen, rows)
    story = scenario_story(world.scenario)
    suggested: set[str] = (
        set(story.suggested_recipients)
        if story is not None and full and focused is not None and _from_chat(focused)
        else set()
    )
    nodes = [
        _node(org, focused, recipients.get(org.id), suggested=org.slug in suggested, full=full)
        for org in orgs
    ]
    titles = await _titles(db, actor, listed, refs)
    return NetworkView(
        world_id=actor.world_id,
        requests=[
            NetworkRequestRef(
                id=row.id,
                title=titles[row.id],
                requester=refs[row.requester_org_id],
                status=requests.request_status(row.status),
                created_at=row.created_at,
            )
            for row in listed
            if row.requester_org_id in refs
        ],
        request_id=focused.id if focused is not None else None,
        nodes=nodes,
        edges=edges,
        counts=NetworkCounts(
            orgs=len(nodes),
            threads=len(edges),
            active=sum(1 for edge in edges if edge.state not in threads.TERMINAL_STATES),
            closed=sum(1 for edge in edges if edge.state in threads.TERMINAL_STATES),
        ),
    )


async def _visible_requests(db: AsyncSession, actor: Actor) -> list[NetworkRequest]:
    """Most recent first. The network lens sees every request; the desk sees requests
    with a thread the member can see, and their own chats' requests."""
    query = repo.in_world(NetworkRequest, actor.world_id)
    if actor.lens != "network":
        with_thread = access.visible_threads(actor).with_only_columns(Thread.request_id)
        own_chats = select(Conversation.id).where(
            Conversation.world_id == actor.world_id,
            Conversation.owner_member_id == actor.member_id,
        )
        query = query.where(
            NetworkRequest.id.in_(with_thread) | NetworkRequest.conversation_id.in_(own_chats)
        )
    return list(
        await db.scalars(
            query.order_by(NetworkRequest.created_at.desc(), NetworkRequest.id).limit(
                REQUESTS_LIMIT
            )
        )
    )


async def _titles(
    db: AsyncSession,
    actor: Actor,
    listed: Sequence[NetworkRequest],
    refs: Mapping[UUID, OrgRef],
) -> dict[UUID, str]:
    """Each listed request's title as the acting member's side names it: the
    requester's own, and a recipient's own thread's, which never shows the request's
    title. The lens shows both sides of every thread, so it reads the request's."""
    if actor.lens == "network":
        return {row.id: threads.requester_title(row) for row in listed}
    received = [row.id for row in listed if row.requester_org_id != actor.org_id]
    own = {
        thread.request_id: thread
        for thread in await db.scalars(
            repo.in_world(Thread, actor.world_id).where(
                Thread.request_id.in_(received), Thread.responder_org_id == actor.org_id
            )
        )
    }
    openings = await threads.opening_lines(db, actor.world_id, [t.id for t in own.values()])
    titles: dict[UUID, str] = {}
    for row in listed:
        requester = refs[row.requester_org_id].name if row.requester_org_id in refs else ""
        thread = own.get(row.id)
        if row.requester_org_id == actor.org_id:
            titles[row.id] = threads.requester_title(row)
        elif thread is not None:
            titles[row.id] = threads.thread_title(
                thread, row, actor.org_id, requester=requester, opening=openings.get(thread.id)
            )
        else:
            titles[row.id] = threads.responder_title(requester=requester, opening=None)
    return titles


def _sees_all_recipients(actor: Actor, request: NetworkRequest) -> bool:
    return actor.lens == "network" or actor.org_id == request.requester_org_id


def _from_chat(request: NetworkRequest) -> bool:
    """Sent from a chat in this world, as opposed to the scenario's seeded history."""
    return request.conversation_id is not None


def _focus(listed: Sequence[NetworkRequest], request_id: UUID | None) -> NetworkRequest | None:
    if request_id is None:
        return next(
            (row for row in listed if row.status == "active" or _from_chat(row)),
            None,
        )
    found = next((row for row in listed if row.id == request_id), None)
    if found is None:
        raise not_found("request")
    return found


def _node(
    org: Org,
    focused: NetworkRequest | None,
    recipient: RecipientStatus | None,
    *,
    suggested: bool,
    full: bool,
) -> NetworkNode:
    role: NodeRole = "bystander"
    if focused is not None and org.id == focused.requester_org_id:
        role = "requester"
    elif recipient is not None:
        role = "recipient"
    return NetworkNode(
        org=org_ref(org),
        kind=org.kind,
        featured=org.featured,
        role=role,
        participant=recipient.participant if recipient is not None else None,
        # The requester's screener verdict is the requester's.
        fit=recipient.fit if recipient is not None and full else None,
        waiting_on=recipient.waiting_on if recipient is not None else None,
        suggested=suggested,
    )


async def _shown_threads(
    db: AsyncSession, actor: Actor, focused: NetworkRequest, *, full: bool
) -> list[Thread]:
    """The focused request's threads; without `full`, only the acting member's org's
    own threads."""
    rows = await requests.threads_of(db, focused)
    if full:
        return rows
    return [row for row in rows if actor.org_id in threads.parties(row)]


async def _seen(db: AsyncSession, actor: Actor, rows: Sequence[Thread]) -> set[UUID]:
    """The threads whose contents the acting member may see: on the desk, those they
    can open (routed or shared); on the network lens, every one."""
    if actor.lens == "network":
        return {row.id for row in rows}
    return set(
        await db.scalars(
            access.visible_threads(actor)
            .where(Thread.id.in_([row.id for row in rows]))
            .with_only_columns(Thread.id)
        )
    )


async def _edges(
    db: AsyncSession,
    actor: Actor,
    rows: Sequence[Thread],
    seen: set[UUID],
    recipients: Sequence[RecipientStatus],
) -> list[NetworkEdge]:
    lines = {row.thread_id: row.latest_line for row in recipients if row.thread_id is not None}
    summaries = await _own_summaries(db, actor, rows)
    return [_edge(row, row.id in seen, lines, summaries) for row in rows]


async def _own_summaries(db: AsyncSession, actor: Actor, rows: Sequence[Thread]) -> dict[UUID, str]:
    """The acting member's org's close summaries for the threads it is party to."""
    sides = {
        row.id: threads.side_of(row, actor.org_id)
        for row in rows
        if actor.org_id in threads.parties(row)
    }
    found = await db.scalars(
        repo.in_world(RepSession, actor.world_id).where(
            RepSession.thread_id.in_(list(sides)), RepSession.close_summary.is_not(None)
        )
    )
    return {
        rep.thread_id: rep.close_summary
        for rep in found
        if rep.close_summary is not None and sides.get(rep.thread_id) == rep.side
    }


def _edge(
    thread: Thread,
    detailed: bool,
    lines: Mapping[UUID, str | None],
    summaries: Mapping[UUID, str],
) -> NetworkEdge:
    return NetworkEdge(
        thread_id=thread.id,
        from_org_id=thread.requester_org_id,
        to_org_id=thread.responder_org_id,
        state=thread.state,
        stop_reason=thread.stop_reason,
        latest_line=lines.get(thread.id) if detailed else None,
        turn_count=thread.turn_count if detailed else None,
        close_summary=summaries.get(thread.id) if detailed else None,
        last_activity_at=thread.last_activity_at,
    )
