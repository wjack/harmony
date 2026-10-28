"""API views of identities: orgs, members (the switchable accounts) and the session."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.config import Settings
from harmony.db.models import Member, Org, World
from harmony.db.models.choices import MemberRole, WorldMode, WorldStatus
from harmony.llm.pricing import HAIKU_5_5, OPUS_5_5
from harmony.llm.requests import (
    HAIKU_DISABLE_THINKING_EFFORTS,
    ROLE_PROFILES,
    Tier,
    resolve_model,
)
from harmony.worlds.service import WorldLens

_ROLE_ORDER: Final[dict[MemberRole, int]] = {"admin": 0, "member": 1}
# How the composer names the model internal Claude runs on.
MODEL_LABELS: Final[Mapping[str, str]] = {OPUS_5_5: "Opus 5.5", HAIKU_5_5: "Haiku 5.5"}
SCRIPTED_LABEL: Final = "Scripted"


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
    # Demo controls' Skip approvals (§11.4).
    skip_approvals: bool


class InternalModelView(_View):
    """The model internal Claude's chats run on for the acting member's org (3.13 E6):
    the composer shows "{label} · Adaptive" when `adaptive`, else the label."""

    id: str
    label: str
    # Its thinking is adaptive (`llm.requests`); never with scripted models.
    adaptive: bool


class SessionView(_View):
    """`GET /api/session`: who the visitor is acting as, and in which world."""

    session_id: UUID
    # Bumped by every act-as and world switch; streams opened under an older epoch close.
    epoch: int
    lens: WorldLens
    world_id: UUID
    world: WorldView
    member: MemberView
    internal_model: InternalModelView


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


def internal_model(org: Org, settings: Settings) -> InternalModelView:
    """What internal Claude runs on for `org`: its tier's model, or `MODEL_OVERRIDE`."""
    tier: Tier = "featured" if org.featured else "background"
    model = resolve_model("internal", tier, settings.model_override)
    if settings.model_mode == "scripted":
        return InternalModelView(id=model, label=SCRIPTED_LABEL, adaptive=False)
    profile = ROLE_PROFILES[("internal", tier)]
    # The request builder disables Haiku 5.5's thinking only for a role that wants it
    # off, at an effort that allows it; every other call thinks adaptively.
    disabled = (
        model == HAIKU_5_5
        and profile.thinking_off
        and profile.effort in HAIKU_DISABLE_THINKING_EFFORTS
    )
    return InternalModelView(id=model, label=MODEL_LABELS.get(model, model), adaptive=not disabled)


async def session_view(
    session: AsyncSession,
    *,
    session_id: UUID,
    epoch: int,
    lens: WorldLens,
    world_id: UUID,
    member_id: UUID,
    settings: Settings,
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
            skip_approvals=world.skip_approvals,
        ),
        member=member_view(member, org),
        internal_model=internal_model(org, settings),
    )
