"""Members routes, mounted under `/api` by `create_app` (ARCHITECTURE.md §11.3).

- GET /api/members: the switchable accounts of the session's world, grouped as
  *Featured* (members of featured orgs) and *All network members* (everyone else)
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from harmony.api.deps import SessionmakerDep, documented, read_cursor, set_cursor, snapshot_session
from harmony.auth.session import ActorDep
from harmony.auth.views import MemberDirectory, member_directory

router = APIRouter(tags=["members"])


@router.get("/members", responses=documented(401, cursor=True))
async def list_members(
    actor: ActorDep, sessionmaker: SessionmakerDep, response: Response
) -> MemberDirectory:
    async with snapshot_session(sessionmaker) as db:
        cursor = await read_cursor(db, actor.world_id)
        directory = await member_directory(db, actor.world_id)
    set_cursor(response, cursor)
    return directory
