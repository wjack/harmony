"""Routing: who an escalation goes to (§10; REVISION_UNSTRUCTURED.md §6).

Routing is prompt-based: the answerer reads the org's routing prompt and member profiles
and proposes a `Route` (member slugs, each with why). Code only validates it: unknown
slugs are dropped, and with nothing valid left the escalation goes to the org's default
assignee (`rep_policy.default_assignee_id`, else its first admin). Nothing is recorded
for routing to learn from; the escalation's snapshot carries the assignees.

Owner: E2.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents.models import Route, RouteAssignee
from harmony.db import repo
from harmony.db.models import Escalation, Member, RepPolicy
from harmony.db.uow import UnitOfWork
from harmony.domain import escalations
from harmony.domain.errors import InvalidRoute

log = logging.getLogger(__name__)


def _slug(ref: str) -> str:
    return ref.strip().removeprefix("@").casefold()


def named_slugs(assignees: Iterable[RouteAssignee], slugs: Iterable[str]) -> dict[str, str]:
    """The member slugs (of `slugs`) that a route's assignees name, each with its why,
    as `apply_route` reads them: compared loosely (case, a leading `@`, spaces), unknown
    ones dropped, each member once (with the first why given for them)."""
    canonical = {_slug(slug): slug for slug in slugs}
    named: dict[str, str] = {}
    for assignee in assignees:
        slug = canonical.get(_slug(assignee.member))
        if slug is not None:
            named.setdefault(slug, assignee.why)
    return named


async def apply_route(uow: UnitOfWork, escalation: Escalation, route: Route | None) -> list[UUID]:
    """Validate `route` against the org's members and assign the escalation
    (`escalations.assign`), falling back to the default assignee. Returns the assignees.
    An escalation that already has assignees keeps them: a retry, or a member
    reassigned it since. The draft keeps the route, so a reviewer can tell an item
    Claude routed from one that fell back."""
    session = uow.session
    members = await _org_members(session, uow.world_id, escalation.org_id)
    if not members:
        raise InvalidRoute(f"org {escalation.org_id} has no members to route to")
    by_slug = {member.slug: member for member in members}
    chosen = [by_slug[slug] for slug in named_slugs(route.assignees if route else [], by_slug)]
    if not chosen:
        chosen = [await _default_assignee(session, uow.world_id, escalation.org_id, members)]
    current = escalations.assignee_ids(await escalations.locked(uow, escalation.id))
    if current:
        return current
    member_ids = [member.id for member in chosen]
    await escalations.assign(uow, escalation.id, member_ids=member_ids)
    log.debug("escalation %s routed to %s", escalation.id, member_ids)
    return member_ids


async def _org_members(session: AsyncSession, world_id: UUID, org_id: UUID) -> list[Member]:
    return list(
        await session.scalars(
            repo.in_world(Member, world_id).where(Member.org_id == org_id).order_by(Member.slug)
        )
    )


async def _default_assignee(
    session: AsyncSession, world_id: UUID, org_id: UUID, members: Sequence[Member]
) -> Member:
    """`rep_policy.default_assignee_id`, else the first admin by slug, else the first
    member by slug."""
    policy = await session.get(RepPolicy, (world_id, org_id))
    if policy is not None and policy.default_assignee_id is not None:
        for member in members:
            if member.id == policy.default_assignee_id:
                return member
    admins = [member for member in members if member.role == "admin"]
    return (admins or list(members))[0]


__all__ = [
    "apply_route",
    "named_slugs",
]
