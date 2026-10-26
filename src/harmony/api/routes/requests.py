"""Requests routes: a network request's live status (§4, §11.6, UI_REFERENCE §5).

- GET /api/requests/{request_id}: per-recipient status (`domain.requests.status`)

Visible to the requester org's members who can see the origin conversation or any of
the request's threads (404 otherwise). The origin chat's owner sees every thread's
detail; anyone else sees what a thread says (its latest line, close summary and who it
waits on) only for the threads they can open, as the network view shows them, and not
the origin chat, which only its owner can open (`requests.scoped`). Carries
`X-Harmony-Cursor`; `network_request`, `thread`, `consult` and `escalation` snapshots
keep it live.

Owner: E4.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.api.deps import SessionmakerDep, read_cursor, set_cursor, snapshot_session
from harmony.api.routes.problems import not_found, problems
from harmony.auth.session import Actor, ActorDep
from harmony.db import repo
from harmony.db.models import NetworkRequest
from harmony.domain import requests
from harmony.domain.requests import RequestStatusView

router = APIRouter(tags=["requests"])


@router.get(
    "/requests/{request_id}",
    response_model=RequestStatusView,
    responses=problems(status.HTTP_404_NOT_FOUND, cursor=True),
)
async def get_request(
    request_id: UUID, actor: ActorDep, sessionmaker: SessionmakerDep, response: Response
) -> RequestStatusView:
    async with snapshot_session(sessionmaker) as db:
        request, readable = await _visible_request(db, actor, request_id)
        view = await requests.status(db, request)
        cursor = await read_cursor(db, actor.world_id)
    set_cursor(response, cursor)
    return requests.scoped(view, readable)


async def _visible_request(
    db: AsyncSession, actor: Actor, request_id: UUID
) -> tuple[NetworkRequest, set[UUID] | None]:
    """The request, if the actor's org sent it and the actor sees its chat or one of its
    threads (404 otherwise), with the threads they can open
    (`requests.readable_threads`)."""
    request = await repo.get(db, NetworkRequest, actor.world_id, request_id)
    if request is None or request.requester_org_id != actor.org_id:
        raise not_found("request")
    readable = await requests.readable_threads(db, actor, request)
    if readable is not None and not readable:
        raise not_found("request")
    return request, readable
