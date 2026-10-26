"""Who can see a thread (ARCHITECTURE.md §10, §11.3).

A member sees a thread when their org is a party to it and they have a `thread_access`
row (routed or shared), or they are an admin of a party org. Threads aren't visible
org-wide by default, because they can carry client-confidential details.

`can_see_thread` states the rule; `visible_to` is the same rule as a SQL predicate, and
every thread query goes through it (`visible_threads`, `get_visible_thread`). A thread
the member can't see is reported exactly like one that doesn't exist in their world, so
the API answers 404, never 403, and an ID reveals nothing.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Final, Protocol
from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.db import repo
from harmony.db.models import Member, Thread, ThreadAccess
from harmony.db.models.choices import AccessReason, MemberRole

# The order a member's reasons are listed in; the first is the one an inbox shows.
REASON_ORDER: Final[tuple[AccessReason, ...]] = ("routed", "shared", "admin")


class Viewer(Protocol):
    """A member acting in one world (`auth.session.Actor` is one)."""

    @property
    def world_id(self) -> UUID: ...

    @property
    def member_id(self) -> UUID: ...

    @property
    def org_id(self) -> UUID: ...

    @property
    def role(self) -> MemberRole: ...


@dataclass(frozen=True, slots=True)
class MemberViewer:
    """A member as a `Viewer` outside an API request: internal Claude reads for the
    member whose chat it is."""

    world_id: UUID
    member_id: UUID
    org_id: UUID
    role: MemberRole

    @classmethod
    def of(cls, member: Member) -> MemberViewer:
        return cls(
            world_id=member.world_id, member_id=member.id, org_id=member.org_id, role=member.role
        )


def is_party(org_id: UUID, thread: Thread) -> bool:
    return org_id in (thread.requester_org_id, thread.responder_org_id)


def can_see_thread(viewer: Viewer, thread: Thread, reasons: Collection[AccessReason]) -> bool:
    """The rule. `reasons` is the viewer's `thread_access` reasons for this thread."""
    if thread.world_id != viewer.world_id or not is_party(viewer.org_id, thread):
        return False
    return viewer.role == "admin" or bool(reasons)


def visible_to(viewer: Viewer) -> ColumnElement[bool]:
    """`can_see_thread` as a predicate on `threads` (add it to any thread query)."""
    party = and_(
        Thread.world_id == viewer.world_id,
        or_(Thread.requester_org_id == viewer.org_id, Thread.responder_org_id == viewer.org_id),
    )
    if viewer.role == "admin":
        return party
    has_access = exists().where(
        ThreadAccess.world_id == Thread.world_id,
        ThreadAccess.thread_id == Thread.id,
        ThreadAccess.member_id == viewer.member_id,
    )
    return and_(party, has_access)


def visible_threads(viewer: Viewer) -> Select[Thread]:
    """Every thread in the viewer's world that they can see; add ordering and filters."""
    return repo.in_world(Thread, viewer.world_id).where(visible_to(viewer))


async def get_visible_thread(session: AsyncSession, viewer: Viewer, thread_id: UUID) -> Thread:
    """The thread, or `repo.NotFoundInWorld` if it's missing or the viewer can't see it."""
    thread = await session.scalar(visible_threads(viewer).where(Thread.id == thread_id))
    if thread is None:
        raise repo.NotFoundInWorld(Thread, viewer.world_id, thread_id)
    return thread


async def access_reasons(
    session: AsyncSession, viewer: Viewer, threads: Iterable[Thread]
) -> dict[UUID, list[AccessReason]]:
    """Why the viewer sees each of `threads` (which they must be able to see), in
    `REASON_ORDER`. An admin's access is listed as `admin` whether or not a row says so."""
    by_id = {thread.id: thread for thread in threads}
    reasons: dict[UUID, set[AccessReason]] = {thread_id: set() for thread_id in by_id}
    if by_id:
        rows = await session.execute(
            select(ThreadAccess.thread_id, ThreadAccess.reason).where(
                ThreadAccess.world_id == viewer.world_id,
                ThreadAccess.member_id == viewer.member_id,
                ThreadAccess.thread_id.in_(by_id),
            )
        )
        for thread_id, reason in rows:
            reasons[thread_id].add(reason)
    if viewer.role == "admin":
        for thread_id, thread in by_id.items():
            if is_party(viewer.org_id, thread):
                reasons[thread_id].add("admin")
    return {
        thread_id: [reason for reason in REASON_ORDER if reason in found]
        for thread_id, found in reasons.items()
    }
