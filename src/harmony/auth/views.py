"""API views of identities: orgs, members (the switchable accounts) and the session."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.db.models import Member, Org, World
from harmony.db.models.choices import MemberRole, WorldMode, WorldStatus
from harmony.worlds.service import WorldLens

_ROLE_ORDER: Final[dict[MemberRole, int]] = {"admin": 0, "member": 1}


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class OrgRef(_View):
    id: UUID
    slug: str
    name: str


class MemberView(_View):
    id: UUID
    slug: str
    name: str
    title: str | None
    role: MemberRole
    org: OrgRef


class WorldView(_View):
    id: UUID
    scenario: str
    mode: WorldMode
    status: WorldStatus
    created_at: datetime
    # World clock: scenario anchor + (now - created_at) + ff_offset_s (§11.1).
    ff_offset_s: int


class SessionView(_View):
    """`GET /api/session`: who the visitor is acting as, and in which world."""

    session_id: UUID
    # Bumped by every act-as and world switch; streams opened under an older epoch close.
    epoch: int
    lens: WorldLens
    world_id: UUID
    world: WorldView
    member: MemberView


class MemberDirectory(_View):
    """`GET /api/members`, grouped as the account switcher shows them (§11.3):
    members of featured orgs, then everyone else in the network."""

    featured: list[MemberView]
    all: list[MemberView]


def org_ref(org: Org) -> OrgRef:
    return OrgRef(id=org.id, slug=org.slug, name=org.name)


def member_view(member: Member, org: Org) -> MemberView:
    return MemberView(
        id=member.id,
        slug=member.slug,
        name=member.name,
        title=member.title,
        role=member.role,
        org=org_ref(org),
    )


async def member_rows(
    session: AsyncSession, world_id: UUID, *where: ColumnElement[bool]
) -> Sequence[tuple[Member, Org]]:
    """Members of the world with their orgs, ordered by org name, role, then name."""
    rows = await session.execute(
        select(Member, Org)
        .join(Org, (Org.world_id == Member.world_id) & (Org.id == Member.org_id))
        .where(Member.world_id == world_id, *where)
    )
    pairs = [(member, org) for member, org in rows]
    return sorted(
        pairs, key=lambda pair: (pair[1].name, pair[1].id, _ROLE_ORDER[pair[0].role], pair[0].name)
    )


async def member_directory(session: AsyncSession, world_id: UUID) -> MemberDirectory:
    featured: list[MemberView] = []
    rest: list[MemberView] = []
    for member, org in await member_rows(session, world_id):
        (featured if org.featured else rest).append(member_view(member, org))
    return MemberDirectory(featured=featured, all=rest)


async def session_view(
    session: AsyncSession,
    *,
    session_id: UUID,
    epoch: int,
    lens: WorldLens,
    world_id: UUID,
    member_id: UUID,
) -> SessionView:
    """The view of a resolved actor. Raises `LookupError` if the world or member is gone."""
    world = await session.get(World, world_id)
    rows = await member_rows(session, world_id, Member.id == member_id)
    if world is None or not rows:
        raise LookupError(f"member {member_id} not found in world {world_id}")
    member, org = rows[0]
    return SessionView(
        session_id=session_id,
        epoch=epoch,
        lens=lens,
        world_id=world_id,
        world=WorldView(
            id=world.id,
            scenario=world.scenario,
            mode=world.mode,
            status=world.status,
            created_at=world.created_at,
            ff_offset_s=world.ff_offset_s,
        ),
        member=member_view(member, org),
    )
