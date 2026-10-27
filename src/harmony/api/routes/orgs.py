"""Org settings routes: the Customize page (REVISION_UNSTRUCTURED.md §3, §9).

- GET /api/orgs/{slug}/harmony: the org's Harmony settings (any member of the org)
- PUT /api/orgs/{slug}/harmony: change them, members' profiles included (admins only:
  403 `not_admin`)
- POST /api/orgs/{slug}/trusted-organizations: add a trusted organization
- PUT /api/orgs/{slug}/trusted-organizations/{counterparty_org_id}: change one
- DELETE /api/orgs/{slug}/trusted-organizations/{counterparty_org_id}?version=: remove one

The writes are `domain.settings` services (versions, live-thread notices, the admin
check). Their errors map to problems here: not an admin is 403 `not_admin`, a stale
version or a duplicate trusted organization is 409 `invalid`, bad input is 422
`invalid`, and an unknown trusted organization is 404. Another org's slug is 404 for
every route, before the admin check. The trusted-organization list is read through
`HarmonySettingsView.trusted_organizations`.

Owner: E4 (U4 builds the page).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents.models import MemberRef
from harmony.api.deps import SessionmakerDep, read_cursor, set_cursor, snapshot_session
from harmony.api.routes.problems import (
    invalid,
    not_found,
    problem,
    problems,
    world_retired,
)
from harmony.auth.session import Actor, ActorDep
from harmony.auth.views import OrgRef, org_ref
from harmony.db import repo
from harmony.db.models import Member, Org, RepPolicy, RepSettings
from harmony.db.models.choices import Autonomy, DirectoryVisibility, MemberRole
from harmony.db.uow import UnitOfWork, unit_of_work
from harmony.domain import settings
from harmony.domain.errors import NotAdmin, SettingsConflict
from harmony.errors import WorldRetired

router = APIRouter(tags=["orgs"])

PUBLIC_SUMMARY_MAX_CHARS: Final = 2000
PROSE_MAX_CHARS: Final = 20_000


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class MemberProfileView(_View):
    member: MemberRef
    role: MemberRole
    title: str | None
    # What they handle, in prose; the answerer reads it when routing (§6).
    profile: str


class TrustedOrganizationView(_View):
    counterparty: OrgRef
    # None: "Same as default".
    autonomy: Autonomy | None
    # "Also share with them: ...", added to the org's default description.
    sharing_description: str
    updated_by: MemberRef | None
    updated_at: datetime
    # Optimistic concurrency for PUT and DELETE.
    version: int


class HarmonySettingsView(_View):
    org: OrgRef
    settings_version: int
    # What counterparties and the discovery screener read.
    public_summary: str
    instructions: str
    # What the representative may pass on to any counterparty.
    sharing_description: str
    # The default mode; a trusted organization may have its own.
    autonomy: Autonomy
    trusted_organizations: list[TrustedOrganizationView]
    directory_visibility: DirectoryVisibility
    routing_prompt: str
    default_assignee: MemberRef | None
    # Internal Claude sends Harmony Network Requests without review by default; a chat
    # may override it (`PATCH /api/conversations/{id}`).
    send_without_confirmation: bool
    members: list[MemberProfileView]
    # The `rep_policy` row's version (optimistic concurrency for PUT).
    policy_version: int
    # Whether the acting member may write.
    editable: bool


class HarmonySettingsUpdate(BaseModel):
    """Omitted fields are unchanged. `policy_version` must match (409 otherwise)."""

    model_config = ConfigDict(extra="forbid")

    policy_version: int = Field(ge=1)
    public_summary: str | None = Field(
        default=None, min_length=1, max_length=PUBLIC_SUMMARY_MAX_CHARS
    )
    instructions: str | None = Field(default=None, max_length=PROSE_MAX_CHARS)
    sharing_description: str | None = Field(default=None, max_length=PROSE_MAX_CHARS)
    autonomy: Autonomy | None = None
    directory_visibility: DirectoryVisibility | None = None
    routing_prompt: str | None = Field(default=None, max_length=PROSE_MAX_CHARS)
    default_assignee_id: UUID | None = None
    send_without_confirmation: bool | None = None
    # Members' profiles to replace, by member id; members left out are unchanged.
    member_profiles: dict[UUID, Annotated[str, Field(max_length=PROSE_MAX_CHARS)]] | None = None


class TrustedOrganizationCreate(BaseModel):
    """Another org of the world, not already trusted (409 `invalid` if it is)."""

    model_config = ConfigDict(extra="forbid")

    counterparty_org_id: UUID
    autonomy: Autonomy | None = None
    sharing_description: str = Field(default="", max_length=PROSE_MAX_CHARS)


class TrustedOrganizationUpdate(BaseModel):
    """Omitted fields are unchanged; `autonomy: null` sets "Same as default"."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    autonomy: Autonomy | None = None
    sharing_description: str | None = Field(default=None, max_length=PROSE_MAX_CHARS)


_WRITE_ERRORS = (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT)


@router.get(
    "/orgs/{slug}/harmony",
    response_model=HarmonySettingsView,
    responses=problems(status.HTTP_404_NOT_FOUND, cursor=True),
)
async def get_harmony_settings(
    slug: str, actor: ActorDep, sessionmaker: SessionmakerDep, response: Response
) -> HarmonySettingsView:
    async with snapshot_session(sessionmaker) as db:
        org = await _own_org(db, actor, slug)
        view = await _settings_view(db, actor, org)
        cursor = await read_cursor(db, actor.world_id)
    set_cursor(response, cursor)
    return view


@router.put(
    "/orgs/{slug}/harmony",
    response_model=HarmonySettingsView,
    responses=problems(*_WRITE_ERRORS),
)
async def update_harmony_settings(
    slug: str, body: HarmonySettingsUpdate, actor: ActorDep, sessionmaker: SessionmakerDep
) -> HarmonySettingsView:
    org_id = await _own_org_id(sessionmaker, actor, slug)
    edit = settings.SettingsEdit.model_validate(
        body.model_dump(exclude_unset=True, exclude={"policy_version"})
    )
    async with _settings_write(sessionmaker, actor) as uow:
        await settings.update(
            uow,
            org_id=org_id,
            member_id=actor.member_id,
            policy_version=body.policy_version,
            edit=edit,
        )
    async with sessionmaker() as db:
        org = await repo.get_required(db, Org, actor.world_id, org_id)
        return await _settings_view(db, actor, org)


@router.post(
    "/orgs/{slug}/trusted-organizations",
    response_model=TrustedOrganizationView,
    responses=problems(*_WRITE_ERRORS),
)
async def add_trusted_organization(
    slug: str, body: TrustedOrganizationCreate, actor: ActorDep, sessionmaker: SessionmakerDep
) -> TrustedOrganizationView:
    org_id = await _own_org_id(sessionmaker, actor, slug)
    async with _settings_write(sessionmaker, actor) as uow:
        await settings.add_trusted(
            uow,
            org_id=org_id,
            member_id=actor.member_id,
            counterparty_org_id=body.counterparty_org_id,
            autonomy=body.autonomy,
            sharing_description=body.sharing_description,
        )
    return await _one_trusted_view(sessionmaker, actor.world_id, org_id, body.counterparty_org_id)


@router.put(
    "/orgs/{slug}/trusted-organizations/{counterparty_org_id}",
    response_model=TrustedOrganizationView,
    responses=problems(*_WRITE_ERRORS),
)
async def update_trusted_organization(
    slug: str,
    counterparty_org_id: UUID,
    body: TrustedOrganizationUpdate,
    actor: ActorDep,
    sessionmaker: SessionmakerDep,
) -> TrustedOrganizationView:
    org_id = await _own_org_id(sessionmaker, actor, slug)
    edit = settings.TrustedEdit.model_validate(
        body.model_dump(exclude_unset=True, exclude={"version"})
    )
    async with _settings_write(sessionmaker, actor) as uow:
        await settings.update_trusted(
            uow,
            org_id=org_id,
            member_id=actor.member_id,
            counterparty_org_id=counterparty_org_id,
            version=body.version,
            edit=edit,
        )
    return await _one_trusted_view(sessionmaker, actor.world_id, org_id, counterparty_org_id)


@router.delete(
    "/orgs/{slug}/trusted-organizations/{counterparty_org_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problems(*_WRITE_ERRORS),
)
async def remove_trusted_organization(
    slug: str,
    counterparty_org_id: UUID,
    version: Annotated[int, Query(ge=1)],
    actor: ActorDep,
    sessionmaker: SessionmakerDep,
) -> None:
    org_id = await _own_org_id(sessionmaker, actor, slug)
    async with _settings_write(sessionmaker, actor) as uow:
        await settings.remove_trusted(
            uow,
            org_id=org_id,
            member_id=actor.member_id,
            counterparty_org_id=counterparty_org_id,
            version=version,
        )


async def _own_org(db: AsyncSession, actor: Actor, slug: str) -> Org:
    """The actor's own org by slug; any other slug is 404."""
    org = await db.scalar(repo.in_world(Org, actor.world_id).where(Org.slug == slug))
    if org is None or org.id != actor.org_id:
        raise not_found("org")
    return org


async def _own_org_id(sessionmaker: SessionmakerDep, actor: Actor, slug: str) -> UUID:
    async with sessionmaker() as db:
        return (await _own_org(db, actor, slug)).id


@asynccontextmanager
async def _settings_write(
    sessionmaker: SessionmakerDep, actor: Actor
) -> AsyncGenerator[UnitOfWork]:
    """A settings write's unit of work, committed on exit, with the domain's errors as
    problems (module docstring)."""
    try:
        async with unit_of_work(sessionmaker, actor.world_id) as uow:
            yield uow
            await uow.commit()
    except WorldRetired:
        raise world_retired() from None
    except NotAdmin:
        raise problem(
            status.HTTP_403_FORBIDDEN, "not_admin", "only an admin can change these settings"
        ) from None
    except SettingsConflict as exc:
        raise problem(status.HTTP_409_CONFLICT, "invalid", str(exc)) from None
    except LookupError:
        raise not_found("trusted organization") from None
    except ValueError as exc:
        raise invalid(str(exc)) from None


# --- Views ----------------------------------------------------------------------------


async def _settings_view(db: AsyncSession, actor: Actor, org: Org) -> HarmonySettingsView:
    world_id = actor.world_id
    current = await db.get(RepSettings, (world_id, org.id, org.current_settings_version))
    policy = await db.get(RepPolicy, (world_id, org.id))
    if current is None or policy is None:
        raise not_found("org settings")
    members = list(
        await db.scalars(
            repo.in_world(Member, world_id)
            .where(Member.org_id == org.id)
            .order_by(Member.created_at, Member.slug)
        )
    )
    refs = {m.id: MemberRef(id=m.id, slug=m.slug, name=m.name) for m in members}
    return HarmonySettingsView(
        org=org_ref(org),
        settings_version=current.version,
        public_summary=current.public_summary,
        instructions=current.instructions,
        sharing_description=policy.sharing_description,
        autonomy=policy.autonomy,
        trusted_organizations=await _trusted_views(db, world_id, org.id, refs),
        directory_visibility=policy.directory_visibility,
        routing_prompt=policy.routing_prompt,
        default_assignee=refs.get(policy.default_assignee_id)
        if policy.default_assignee_id is not None
        else None,
        send_without_confirmation=policy.send_without_confirmation,
        members=[
            MemberProfileView(member=refs[m.id], role=m.role, title=m.title, profile=m.profile)
            for m in members
        ],
        policy_version=policy.version,
        editable=actor.role == "admin",
    )


async def _trusted_views(
    db: AsyncSession, world_id: UUID, org_id: UUID, members: Mapping[UUID, MemberRef]
) -> list[TrustedOrganizationView]:
    """The org's trusted organizations, by counterparty name."""
    trusted = await settings.trusted_organizations(db, world_id, org_id)
    orgs = {
        org.id: org_ref(org)
        for org in await repo.list_in_world(db, Org, world_id, Org.id.in_(list(trusted)))
    }
    rows = sorted(
        (row for row in trusted.values() if row.counterparty_org_id in orgs),
        key=lambda row: orgs[row.counterparty_org_id].name,
    )
    return [
        TrustedOrganizationView(
            counterparty=orgs[row.counterparty_org_id],
            autonomy=row.autonomy,
            sharing_description=row.sharing_description,
            updated_by=members.get(row.updated_by_member_id)
            if row.updated_by_member_id is not None
            else None,
            updated_at=row.updated_at,
            version=row.version,
        )
        for row in rows
    ]


async def _one_trusted_view(
    sessionmaker: SessionmakerDep, world_id: UUID, org_id: UUID, counterparty_org_id: UUID
) -> TrustedOrganizationView:
    async with sessionmaker() as db:
        members = await repo.list_in_world(db, Member, world_id, Member.org_id == org_id)
        refs = {m.id: MemberRef(id=m.id, slug=m.slug, name=m.name) for m in members}
        views = await _trusted_views(db, world_id, org_id, refs)
    for view in views:
        if view.counterparty.id == counterparty_org_id:
            return view
    raise not_found("trusted organization")
