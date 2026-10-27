"""Harmony settings: an org's prose settings, live policy and trusted organizations
(REVISION_UNSTRUCTURED.md §2, §3, §6).

`rep_settings` rows (public summary, instructions) are immutable versions: a change
writes the next one, and live threads keep the version their sides froze, so each gets
a `settings` notice. `rep_policy` and `trusted_organizations` are read live at every
decision, so an edit applies at the next consult; a live thread whose mode or
applicable sharing descriptions changed gets a `mode` or `sharing` notice, because the
representative's frozen prompt states what it may pass on (§2).

Members' profiles (what each person handles, in prose) are edited with the rest: the
answerer reads them live when routing (§6), so a change needs no notice.

Every write is admin-only (`NotAdmin`), takes the org's `rep_policy` row lock (so one
org's settings writes and their notices serialize) and is versioned: a stale version,
or trusting an organization twice, raises `SettingsConflict`. Input the API can't
validate on its own (a default assignee or profile for someone outside the org, an
unknown counterparty, a null for a required field) raises `ValueError`; an unknown
trusted organization raises `LookupError`. Snapshots go to `EventScope.org(org)`;
removing a trusted organization sends one last `trusted_organization` snapshot with
`deleted: true`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Final, NamedTuple
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents import context
from harmony.agents.release import mode_for
from harmony.db import repo
from harmony.db.models import Member, Org, RepPolicy, RepSettings, Thread, TrustedOrganization
from harmony.db.models.choices import Autonomy, DirectoryVisibility, Side
from harmony.db.uow import UnitOfWork
from harmony.domain import threads
from harmony.domain.errors import NotAdmin, SettingsConflict
from harmony.events.schemas import EntitySnapshot, EventScope

TRUSTED_ORGANIZATION_ENTITY: Final = "trusted_organization"
SETTINGS_NOTICE: Final = (
    "Notice from Harmony: your organization updated its Harmony settings. This "
    "conversation keeps the instructions it started with."
)
_MODE_EFFECT: Final[Mapping[Autonomy, str]] = {
    "ask": "a teammate now reviews every answer from your side before it reaches you",
    "auto": (
        "answers from your side that stay within what you may pass on now reach you "
        "directly; a teammate reviews the rest"
    ),
    "skip": "answers from your side now reach you without review unless one needs a person",
}
# The one setting an edit may clear (routing then falls back to an admin).
_NULLABLE: Final = frozenset({"default_assignee_id"})


class _Edit(BaseModel):
    """Only the fields in `model_fields_set` change."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class SettingsEdit(_Edit):
    # `rep_settings`: a new version.
    public_summary: str | None = None
    instructions: str | None = None
    # `rep_policy`: in place.
    sharing_description: str | None = None
    autonomy: Autonomy | None = None
    directory_visibility: DirectoryVisibility | None = None
    routing_prompt: str | None = None
    default_assignee_id: UUID | None = None
    # Internal Claude's requests go out without review (3.10). Not in any
    # representative's prompt, so a change sends no notice.
    send_without_confirmation: bool | None = None
    # `members.profile`, by member id: in place.
    member_profiles: dict[UUID, str] | None = None


class TrustedEdit(_Edit):
    # None: the org's default mode.
    autonomy: Autonomy | None = None
    sharing_description: str | None = None


_PROSE_FIELDS: Final = frozenset({"public_summary", "instructions"})
_POLICY_FIELDS: Final = frozenset(SettingsEdit.model_fields) - _PROSE_FIELDS - {"member_profiles"}


async def update(
    uow: UnitOfWork, *, org_id: UUID, member_id: UUID, policy_version: int, edit: SettingsEdit
) -> None:
    """Apply `edit` to the org's settings. `policy_version` is the `rep_policy` version
    the admin edited (`SettingsConflict` when another write bumped it)."""
    session = uow.session
    policy = await _admin_lock(uow, org_id, member_id)
    if policy.version != policy_version:
        raise SettingsConflict(
            f"these settings changed (version {policy.version}); reload and try again"
        )
    prose = edit.model_dump(include=edit.model_fields_set & _PROSE_FIELDS)
    changes = edit.model_dump(include=edit.model_fields_set & _POLICY_FIELDS)
    nulled = sorted(
        field
        for field, value in (prose | changes).items()
        if value is None and field not in _NULLABLE
    )
    if "member_profiles" in edit.model_fields_set and edit.member_profiles is None:
        nulled.append("member_profiles")
    if nulled:
        raise ValueError(f"{', '.join(nulled)} can't be null")
    if edit.default_assignee_id is not None:
        member = await repo.get(session, Member, uow.world_id, edit.default_assignee_id)
        if member is None or member.org_id != org_id:
            raise ValueError("the default assignee must be a member of this org")
    if edit.member_profiles:
        await _set_profiles(uow, org_id, edit.member_profiles)
    if prose and await _new_settings_version(uow, org_id, member_id, prose):
        for thread in await _live_threads(session, uow.world_id, org_id):
            await context.queue_notice(
                uow,
                thread_id=thread.id,
                side=threads.side_of(thread, org_id),
                kind="settings",
                text=SETTINGS_NOTICE,
            )
    changed = {f: value for f, value in changes.items() if getattr(policy, f) != value}
    if not changed:
        return
    async with _term_notices(uow, org_id):
        for field, value in changed.items():
            setattr(policy, field, value)
    uow.add_snapshot(policy, EventScope.org(org_id))


async def mode_toward(
    session: AsyncSession, *, world_id: UUID, org_id: UUID, counterparty_org_id: UUID
) -> Autonomy:
    """The org's mode toward one counterparty, read live (§3). An org without a policy
    row has no mode set, so every answer and message waits for a person."""
    policy = await session.get(RepPolicy, (world_id, org_id))
    if policy is None:
        return "ask"
    trusted = await session.get(TrustedOrganization, (world_id, org_id, counterparty_org_id))
    return mode_for(policy, trusted)


async def trusted_organizations(
    session: AsyncSession, world_id: UUID, org_id: UUID
) -> dict[UUID, TrustedOrganization]:
    """The org's trusted organizations by counterparty id."""
    rows = await repo.list_in_world(
        session, TrustedOrganization, world_id, TrustedOrganization.org_id == org_id
    )
    return {row.counterparty_org_id: row for row in rows}


async def add_trusted(
    uow: UnitOfWork,
    *,
    org_id: UUID,
    member_id: UUID,
    counterparty_org_id: UUID,
    autonomy: Autonomy | None,
    sharing_description: str,
) -> TrustedOrganization:
    """Trust another org of the world."""
    session = uow.session
    await _admin_lock(uow, org_id, member_id)
    counterparty = await repo.get(session, Org, uow.world_id, counterparty_org_id)
    if counterparty is None or counterparty_org_id == org_id:
        raise ValueError("a trusted organization must be another organization on the network")
    if await session.get(TrustedOrganization, (uow.world_id, org_id, counterparty_org_id)):
        raise SettingsConflict(f"{counterparty.name} is already a trusted organization")
    row = TrustedOrganization(
        world_id=uow.world_id,
        org_id=org_id,
        counterparty_org_id=counterparty_org_id,
        autonomy=autonomy,
        sharing_description=sharing_description,
        updated_by_member_id=member_id,
    )
    async with _term_notices(uow, org_id):
        session.add(row)
    await session.refresh(row)
    uow.add_snapshot(row, EventScope.org(org_id))
    return row


async def update_trusted(
    uow: UnitOfWork,
    *,
    org_id: UUID,
    member_id: UUID,
    counterparty_org_id: UUID,
    version: int,
    edit: TrustedEdit,
) -> TrustedOrganization:
    """Change a trusted organization's mode or sharing description."""
    row = await _trusted_locked(uow, org_id, member_id, counterparty_org_id, version)
    changes = edit.model_dump(include=edit.model_fields_set)
    if "sharing_description" in changes and changes["sharing_description"] is None:
        raise ValueError("sharing_description can't be null; send an empty string to clear it")
    changed = {f: value for f, value in changes.items() if getattr(row, f) != value}
    if not changed:
        return row
    async with _term_notices(uow, org_id):
        for field, value in changed.items():
            setattr(row, field, value)
        row.updated_by_member_id = member_id
    await uow.session.refresh(row)
    uow.add_snapshot(row, EventScope.org(org_id))
    return row


async def remove_trusted(
    uow: UnitOfWork, *, org_id: UUID, member_id: UUID, counterparty_org_id: UUID, version: int
) -> None:
    """Stop trusting an org: its threads fall back to the org's defaults."""
    row = await _trusted_locked(uow, org_id, member_id, counterparty_org_id, version)
    snapshot_id, last_version = row.snapshot_id(), row.version
    async with _term_notices(uow, org_id):
        await uow.session.delete(row)
    uow.add_event(
        EntitySnapshot(
            entity_type=TRUSTED_ORGANIZATION_ENTITY,
            entity_id=snapshot_id,
            version=last_version + 1,
            data={
                "org_id": str(org_id),
                "counterparty_org_id": str(counterparty_org_id),
                "deleted": True,
            },
        ),
        EventScope.org(org_id),
    )


def sharing_notice(parts: Sequence[str], *, side: Side, counterparty_name: str) -> str:
    """The `sharing` notice: the descriptions that now apply (as `context.sharing_parts`
    quotes them), replacing the ones in the representative's prompt."""
    if not parts:
        return (
            "Notice from Harmony: your organization no longer describes what you may pass "
            "on to this organization. From now on, pass on only "
            f"{context.also_passes(side, counterparty_name, summary=True)}."
        )
    return (
        "Notice from Harmony: your organization changed what you may pass on to this "
        "organization. It now reads as follows, in place of what your prompt says:\n\n"
        + context.sharing_text(parts, counterparty_name=counterparty_name)
    )


def mode_notice(mode: Autonomy) -> str:
    """The `mode` notice: how consults are answered for this counterparty now."""
    return f"Notice from Harmony: for this conversation, {_MODE_EFFECT[mode]}."


# --- Internals ------------------------------------------------------------------------


async def _admin_lock(uow: UnitOfWork, org_id: UUID, member_id: UUID) -> RepPolicy:
    """The org's `rep_policy`, row-locked, after checking `member_id` is its admin."""
    session = uow.session
    member = await repo.get(session, Member, uow.world_id, member_id)
    if member is None or member.org_id != org_id or member.role != "admin":
        raise NotAdmin(f"member {member_id} isn't an admin of org {org_id}")
    policy = await session.get(
        RepPolicy, (uow.world_id, org_id), with_for_update=True, populate_existing=True
    )
    if policy is None:
        raise LookupError(f"org {org_id} has no rep_policy")
    return policy


async def _set_profiles(uow: UnitOfWork, org_id: UUID, profiles: Mapping[UUID, str]) -> None:
    members = await repo.list_in_world(
        uow.session, Member, uow.world_id, Member.id.in_(list(profiles)), Member.org_id == org_id
    )
    if len(members) != len(profiles):
        raise ValueError("a profile can only be set for a member of this org")
    for member in members:
        if member.profile != profiles[member.id]:
            member.profile = profiles[member.id]
            uow.add_snapshot(member, EventScope.org(org_id))


async def _trusted_locked(
    uow: UnitOfWork, org_id: UUID, member_id: UUID, counterparty_org_id: UUID, version: int
) -> TrustedOrganization:
    await _admin_lock(uow, org_id, member_id)
    row = await uow.session.get(
        TrustedOrganization,
        (uow.world_id, org_id, counterparty_org_id),
        with_for_update=True,
        populate_existing=True,
    )
    if row is None:
        raise LookupError(f"org {org_id} doesn't trust org {counterparty_org_id}")
    if row.version != version:
        raise SettingsConflict(
            f"this trusted organization changed (version {row.version}); reload and try again"
        )
    return row


async def _new_settings_version(
    uow: UnitOfWork, org_id: UUID, member_id: UUID, prose: Mapping[str, str]
) -> bool:
    """Write the next `rep_settings` version; False when nothing would change."""
    session = uow.session
    org = await session.get(Org, (uow.world_id, org_id), with_for_update=True)
    if org is None:
        raise repo.NotFoundInWorld(Org, uow.world_id, org_id)
    current = await session.get(RepSettings, (uow.world_id, org_id, org.current_settings_version))
    if current is None:
        raise LookupError(f"org {org_id} has no settings version {org.current_settings_version}")
    summary = prose.get("public_summary", current.public_summary)
    instructions = prose.get("instructions", current.instructions)
    if (summary, instructions) == (current.public_summary, current.instructions):
        return False
    row = RepSettings(
        world_id=uow.world_id,
        org_id=org_id,
        version=current.version + 1,
        public_summary=summary,
        instructions=instructions,
        created_by_member_id=member_id,
    )
    session.add(row)
    org.current_settings_version = row.version
    await session.flush()
    uow.add_snapshot(row, EventScope.org(org_id))
    uow.add_snapshot(org, EventScope.org(org_id))
    return True


class _Terms(NamedTuple):
    """What one live thread's representative works under: its mode and the sharing
    descriptions that apply, as quoted parts."""

    mode: Autonomy
    sharing: list[str]


@asynccontextmanager
async def _term_notices(uow: UnitOfWork, org_id: UUID) -> AsyncGenerator[None]:
    """Run a policy or trusted-organization change, then tell each live thread whose
    terms it changed."""
    session = uow.session
    live = await _live_threads(session, uow.world_id, org_id)
    names = await _counterparty_names(session, uow.world_id, org_id, live)
    before = await _terms(session, uow.world_id, org_id, live, names)
    yield
    await session.flush()
    after = await _terms(session, uow.world_id, org_id, live, names)
    for thread in live:
        side = threads.side_of(thread, org_id)
        old, new = before[thread.id], after[thread.id]
        if new.mode != old.mode:
            await context.queue_notice(
                uow, thread_id=thread.id, side=side, kind="mode", text=mode_notice(new.mode)
            )
        if new.sharing != old.sharing:
            await context.queue_notice(
                uow,
                thread_id=thread.id,
                side=side,
                kind="sharing",
                text=sharing_notice(
                    new.sharing, side=side, counterparty_name=names[_counterparty(thread, org_id)]
                ),
            )


async def _terms(
    session: AsyncSession,
    world_id: UUID,
    org_id: UUID,
    live: Sequence[Thread],
    names: Mapping[UUID, str],
) -> dict[UUID, _Terms]:
    policy = await session.get(RepPolicy, (world_id, org_id))
    if policy is None:
        raise LookupError(f"org {org_id} has no rep_policy")
    trusted = await trusted_organizations(session, world_id, org_id)
    terms: dict[UUID, _Terms] = {}
    for thread in live:
        counterparty = _counterparty(thread, org_id)
        row = trusted.get(counterparty)
        terms[thread.id] = _Terms(
            mode=mode_for(policy, row),
            sharing=context.sharing_parts(policy, row, counterparty_name=names[counterparty]),
        )
    return terms


async def _live_threads(session: AsyncSession, world_id: UUID, org_id: UUID) -> list[Thread]:
    return list(
        await repo.list_in_world(
            session,
            Thread,
            world_id,
            (Thread.requester_org_id == org_id) | (Thread.responder_org_id == org_id),
            Thread.state.not_in(threads.TERMINAL_STATES),
        )
    )


async def _counterparty_names(
    session: AsyncSession, world_id: UUID, org_id: UUID, live: Sequence[Thread]
) -> dict[UUID, str]:
    ids = {_counterparty(thread, org_id) for thread in live}
    if not ids:
        return {}
    orgs = await repo.list_in_world(session, Org, world_id, Org.id.in_(ids))
    return {org.id: org.name for org in orgs}


def _counterparty(thread: Thread, org_id: UUID) -> UUID:
    return threads.org_of(thread, threads.other(threads.side_of(thread, org_id)))


__all__ = [
    "SETTINGS_NOTICE",
    "TRUSTED_ORGANIZATION_ENTITY",
    "SettingsEdit",
    "TrustedEdit",
    "add_trusted",
    "mode_notice",
    "mode_toward",
    "remove_trusted",
    "sharing_notice",
    "trusted_organizations",
    "update",
    "update_trusted",
]
