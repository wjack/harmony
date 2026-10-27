"""The check every write that names its tab's member runs first (`as_member_id`: notes,
steers, reopen; docs/design/EDIT.md §3.1, docs/design/REOPEN.md §4.2).

Act-as only updates the session row, so a tab still showing Helen after a switch in
another tab would otherwise write as the new member, and as their org. A mismatch is
409 `member_changed` with `member`, the session's member now, before the route reads
or writes anything of the thread's.

Owner: E4.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.api.routes.problems import member_ref, problem
from harmony.auth.session import Actor


async def require_acting(
    sessionmaker: async_sessionmaker[AsyncSession], actor: Actor, as_member_id: UUID
) -> None:
    """Return when the session still acts as `as_member_id`; otherwise raise 409
    `member_changed` naming the member it acts as now."""
    if actor.member_id == as_member_id:
        return
    async with sessionmaker() as session:
        member = await member_ref(session, actor.world_id, actor.member_id)
    raise problem(
        status.HTTP_409_CONFLICT,
        "member_changed",
        "you switched to another member in another tab",
        member=member,
    )


__all__ = ["require_acting"]
