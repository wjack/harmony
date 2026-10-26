"""Worlds routes, mounted under `/api` by `create_app` (ARCHITECTURE.md §7, §11.1).

- POST /api/worlds/reset: fork the session's world into a fresh one; retire the old one
- POST /api/admin/wipe: retire every world (header `X-Admin-Secret: <ADMIN_SECRET>`)
"""

from __future__ import annotations

import hmac
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict

from harmony.api.deps import SessionmakerDep, SettingsDep, WorldServiceDep, documented
from harmony.api.routes.session import session_response
from harmony.auth.session import ActorDep, resolve_actor
from harmony.auth.views import SessionView
from harmony.db import repo
from harmony.db.models.choices import WorldStatus
from harmony.worlds.service import NoWorld

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


@router.post("/worlds/reset", responses=documented(401, cursor=True))
async def reset_world(
    actor: ActorDep,
    worlds: WorldServiceDep,
    sessionmaker: SessionmakerDep,
    response: Response,
) -> ResetResult:
    """Fork a fresh world from the same scenario, keeping the acting member's slug and
    the session's budget lineage; the old world is retired behind it. Bumps the epoch."""
    try:
        switch = await worlds.fork_and_reset(
            actor.session_id, actor.lens, observed_world_id=actor.world_id
        )
    except NoWorld:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "this world is gone; POST /api/session/start"
        ) from None
    current = await resolve_actor(sessionmaker, actor.session_id, actor.lens)
    if current is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "this world is gone; POST /api/session/start"
        )
    view = await session_response(sessionmaker, response, current)
    async with sessionmaker() as db:
        previous_status = await repo.world_status(db, switch.previous_world_id or actor.world_id)
    return ResetResult(
        session=view,
        previous_world_id=switch.previous_world_id or actor.world_id,
        previous_world_status=previous_status,
    )


@router.post("/admin/wipe", responses=documented(403))
async def wipe(
    settings: SettingsDep,
    worlds: WorldServiceDep,
    x_admin_secret: Annotated[str | None, Header()] = None,
) -> WipeResult:
    """Retire every world, one world at a time. Refused unless `X-Admin-Secret` matches
    ADMIN_SECRET; with ADMIN_SECRET unset it is always refused."""
    expected = settings.admin_secret.get_secret_value() if settings.admin_secret else None
    if (
        expected is None
        or x_admin_secret is None
        or not hmac.compare_digest(x_admin_secret.encode(), expected.encode())
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin secret required")
    return WipeResult(retired_world_ids=await worlds.retire_all())
