"""Worlds routes, mounted under `/api` by `create_app` (ARCHITECTURE.md §7, §11.1).

- POST /api/worlds/reset: fork the session's world into a fresh one; retire the old one
- POST /api/admin/wipe: retire every world (header `X-Admin-Secret: <ADMIN_SECRET>`)

While the passcode gate is on, Resets count against the gate's per-session limit
(LIVE_DEMO_PLAN 3.3).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from harmony.api.deps import SessionmakerDep, SettingsDep, WorldServiceDep, documented
from harmony.api.gate import GateDep, too_many_attempts
from harmony.api.routes.admin import AdminSecret, require_admin
from harmony.api.routes.problems import problem_entries
from harmony.api.routes.session import session_response
from harmony.auth.session import ActorDep, resolve_actor
from harmony.auth.views import SessionView
from harmony.db import repo
from harmony.db.models.choices import WorldStatus
from harmony.worlds.service import NoWorld, TooManyWorlds

router = APIRouter(tags=["worlds"])


class ResetResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    session: SessionView
    previous_world_id: UUID
    # `retiring` while retirement runs in the background, `retired` once done.
    previous_world_status: WorldStatus | None


class WipeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    retired_world_ids: list[UUID]


# 429 `too_many_attempts`: too many Resets for this session while the gate is on (3.3).
@router.post(
    "/worlds/reset",
    response_model=ResetResult,
    responses=documented(401, cursor=True) | problem_entries(429),
)
async def reset_world(
    actor: ActorDep,
    worlds: WorldServiceDep,
    sessionmaker: SessionmakerDep,
    settings: SettingsDep,
    gate: GateDep,
    response: Response,
) -> ResetResult | JSONResponse:
    """Fork a fresh world from the same scenario, keeping the acting member's slug and
    the session's budget lineage; the old world is retired behind it. Bumps the epoch."""
    try:
        switch = await worlds.fork_and_reset(
            actor.session_id,
            actor.lens,
            observed_world_id=actor.world_id,
            limits=gate.worlds if gate is not None else None,
        )
    except TooManyWorlds as err:
        return too_many_attempts(err.retry_after_s, "too many resets; try again later")
    except NoWorld:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "this world is gone; POST /api/session/start"
        ) from None
    current = await resolve_actor(sessionmaker, actor.session_id, actor.lens)
    if current is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "this world is gone; POST /api/session/start"
        )
    view = await session_response(sessionmaker, response, current, settings)
    async with sessionmaker() as db:
        previous_status = await repo.world_status(db, switch.previous_world_id or actor.world_id)
    return ResetResult(
        session=view,
        previous_world_id=switch.previous_world_id or actor.world_id,
        previous_world_status=previous_status,
    )


@router.post(
    "/admin/wipe",
    responses=problem_entries(status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
)
async def wipe(
    settings: SettingsDep,
    worlds: WorldServiceDep,
    x_admin_secret: AdminSecret = None,
) -> WipeResult:
    """Retire every world, one world at a time. Refused with 403 `not_admin`, like the
    other operator routes, unless `X-Admin-Secret` matches ADMIN_SECRET; with
    ADMIN_SECRET unset it is always refused."""
    require_admin(settings, x_admin_secret)
    return WipeResult(retired_world_ids=await worlds.retire_all())
