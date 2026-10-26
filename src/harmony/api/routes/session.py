"""Session routes, mounted under `/api` by `create_app` (ARCHITECTURE.md §11.1, §11.3).

- GET /api/session: who the visitor is acting as, and in which world
- POST /api/session/start: create the session and its world lazily (never on page view)
- POST /api/session/act-as {member_id}: switch accounts within the session's world

Every route takes `?world=desk|network` (default desk). Responses carry
`X-Harmony-Cursor`.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.api.deps import (
    ServicesDep,
    SessionmakerDep,
    WorldServiceDep,
    documented,
    read_cursor,
    set_cursor,
    snapshot_session,
)
from harmony.auth.session import (
    Actor,
    ActorDep,
    MemberNotInWorld,
    SessionMoved,
    act_as,
    resolve_actor,
    session_id_from_request,
    set_session_cookie,
)
from harmony.auth.views import SessionView, session_view
from harmony.errors import WorldRetired
from harmony.seeds.loader import ScenarioNotFound, available_scenarios
from harmony.worlds.service import WorldLens

router = APIRouter(tags=["session"])


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: str | None = Field(
        default=None,
        description="Scenario to seed if a world is created (default harmony-demo).",
    )


class ActAsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_id: UUID


async def session_response(
    sessionmaker: async_sessionmaker[AsyncSession], response: Response, actor: Actor
) -> SessionView:
    """The actor's `SessionView`, with its cursor header."""
    async with snapshot_session(sessionmaker) as db:
        cursor = await read_cursor(db, actor.world_id)
        try:
            view = await session_view(
                db,
                session_id=actor.session_id,
                epoch=actor.epoch,
                lens=actor.lens,
                world_id=actor.world_id,
                member_id=actor.member_id,
            )
        except LookupError:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "this world is gone; POST /api/session/start"
            ) from None
    set_cursor(response, cursor)
    return view


@router.get("/session", responses=documented(401, cursor=True))
async def get_session(
    actor: ActorDep, sessionmaker: SessionmakerDep, response: Response
) -> SessionView:
    return await session_response(sessionmaker, response, actor)


@router.post("/session/start", responses=documented(400, 409, cursor=True))
async def start_session(
    request: Request,
    response: Response,
    services: ServicesDep,
    worlds: WorldServiceDep,
    body: StartRequest | None = None,
    world: WorldLens = "desk",
) -> SessionView:
    """Create the visitor's session and its `world` world if they don't exist yet.

    Idempotent for a session that already has a live world. Sets the session cookie.
    """
    settings = services.settings
    scenario = body.scenario if body is not None else None
    try:
        session_id = await worlds.start(
            session_id_from_request(request, settings), lens=world, scenario=scenario
        )
    except ScenarioNotFound as err:
        # Only a scenario named in this request can be unknown: one stored on the
        # session's old world falls back to the default (`WorldService.start`).
        known = ", ".join(available_scenarios())
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"unknown scenario {err.name!r}; available: {known}"
        ) from None
    actor = await resolve_actor(services.sessionmaker, session_id, world)
    if actor is None:
        # Only if the new world was wiped between the two statements.
        raise HTTPException(status.HTTP_409_CONFLICT, "the world was retired; try again")
    set_session_cookie(response, session_id, settings)
    return await session_response(services.sessionmaker, response, actor)


@router.post("/session/act-as", responses=documented(401, 403, 409, cursor=True))
async def act_as_member(
    body: ActAsRequest, actor: ActorDep, sessionmaker: SessionmakerDep, response: Response
) -> SessionView:
    """Act as another member of the session's world. Bumps the epoch, so the session's
    open event streams close; the client reopens them from this response's cursor."""
    try:
        await act_as(sessionmaker, actor, body.member_id)
    except MemberNotInWorld:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "that member is not in this world") from None
    except (SessionMoved, WorldRetired):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "the session changed worlds; reload"
        ) from None
    switched = await resolve_actor(sessionmaker, actor.session_id, actor.lens)
    if switched is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "this world is gone; POST /api/session/start"
        )
    return await session_response(sessionmaker, response, switched)
