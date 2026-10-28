"""Network route: the network view's graph (§11, UI_REFERENCE §8).

- GET /api/network?request=<id>: orgs as nodes and threads as edges

Read in whichever world `current_actor` resolves (`?world=desk|network`). Without
`request`, the world's most recent request sent from a chat is focused: a world's seeded
requests, live or not, are there to pick, not to open on. Nodes are the
world's orgs (bystanders included, for the "whole network" look); edges are the focused
request's threads, with each recipient's participant status.

The network view is a demo tool, opened from the demo bar, not part of the product: it
watches the whole world from outside, so it sees every request, every recipient with
the screener's `fit`, every side's escalations (`needs_human`, `waiting_on`) and every
thread's latest line. Any visitor can already act as anyone, so this exposes nothing
new; the product's own views (chats, Requests, the status card) keep the firewall. On a
request sent from a chat it carries the story's `suggested` marks (seeded requests had
no card to suggest anyone on). An edge's close summary is the acting org's own where it
is a party, else the requester's. Carries `X-Harmony-Cursor`; the demo lens stream
(`GET /api/events?lens=network`) keeps it live.

Owner: E4 (U4 builds the view).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents.models import ParticipantStatus, RequestStatus
from harmony.api.deps import SessionmakerDep, read_cursor, set_cursor, snapshot_session
from harmony.api.routes.demo import scenario_story
from harmony.api.routes.problems import not_found, problems
from harmony.auth.session import Actor, ActorDep
from harmony.auth.views import OrgRef, org_ref
from harmony.db import repo
from harmony.db.models import NetworkRequest, Org, RepSession, Thread, World
from harmony.db.models.choices import EndReason, FailureReason, Fit, Side, ThreadState
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
    # Who the recipient's thread waits on, on either side.
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
    # How the current run ended: status, like `state`.
    end_reason: EndReason | None
    # Which side closed it (or refused).
    closed_by: Side | None
    latest_line: str | None
    turn_count: int | None
    # The acting org's own close summary where it is a party, else the requester's.
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
    listed = await _requests(db, actor)
    focused = _focus(listed, request_id)
    recipients: dict[UUID, RecipientStatus] = {}
    edges: list[NetworkEdge] = []
    if focused is not None:
        view = await requests.status(db, focused, waiting_of="any")
        recipients = {row.org.id: row for row in view.recipients}
        edges = await _edges(db, actor, await requests.threads_of(db, focused), view.recipients)
    story = scenario_story(world.scenario)
    suggested: set[str] = (
        set(story.suggested_recipients)
        if story is not None and focused is not None and _from_chat(focused)
        else set()
    )
    nodes = [
        _node(org, focused, recipients.get(org.id), suggested=org.slug in suggested) for org in orgs
    ]
    return NetworkView(
        world_id=actor.world_id,
        requests=[
            NetworkRequestRef(
                id=row.id,
                title=threads.requester_title(row),
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


async def _requests(db: AsyncSession, actor: Actor) -> list[NetworkRequest]:
    """Every request in the world, most recent first."""
    return list(
        await db.scalars(
            repo.in_world(NetworkRequest, actor.world_id)
            .order_by(NetworkRequest.created_at.desc(), NetworkRequest.id)
            .limit(REQUESTS_LIMIT)
        )
    )


def _from_chat(request: NetworkRequest) -> bool:
    """Sent from a chat in this world, as opposed to the scenario's seeded history."""
    return request.conversation_id is not None


def _focus(listed: Sequence[NetworkRequest], request_id: UUID | None) -> NetworkRequest | None:
    if request_id is None:
        return next((row for row in listed if _from_chat(row)), None)
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
        fit=recipient.fit if recipient is not None else None,
        waiting_on=recipient.waiting_on if recipient is not None else None,
        suggested=suggested,
    )


async def _edges(
    db: AsyncSession,
    actor: Actor,
    rows: Sequence[Thread],
    recipients: Sequence[RecipientStatus],
) -> list[NetworkEdge]:
    lines = {row.thread_id: row.latest_line for row in recipients if row.thread_id is not None}
    summaries = {
        row.thread_id: row.close_summary
        for row in recipients
        if row.thread_id is not None and row.close_summary is not None
    }
    summaries |= await _own_summaries(db, actor, rows)
    return [_edge(row, lines, summaries) for row in rows]


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
    thread: Thread, lines: Mapping[UUID, str | None], summaries: Mapping[UUID, str]
) -> NetworkEdge:
    return NetworkEdge(
        thread_id=thread.id,
        from_org_id=thread.requester_org_id,
        to_org_id=thread.responder_org_id,
        state=thread.state,
        stop_reason=thread.stop_reason,
        end_reason=thread.end_reason,
        closed_by=thread.closed_by,
        latest_line=lines.get(thread.id),
        turn_count=thread.turn_count,
        close_summary=summaries.get(thread.id),
        last_activity_at=thread.last_activity_at,
    )
