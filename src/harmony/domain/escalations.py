"""Escalations: the inbox (§7, §10; REVISION_UNSTRUCTURED.md §4, §6).

An escalation is something waiting on a person: a consult answer the release decision
didn't release (`consult`), or an outgoing message held because the org reviews every
message in *Ask* (`outbound_review`).

`status`: `open` → `resolved` | `cancelled` (the thread ended first). Resolution is
first-writer-wins: `resolve` updates `WHERE status = 'open'` and raises
`domain.errors.EscalationNotOpen` (with who resolved it) when it matched no row. Only a
member of the escalation's org may resolve or reassign it (`NotInOrg`). Approving
releases the answer, or sends the message, to its one thread; nothing is remembered
for other threads (§4.5).

Drafts (`escalation_drafts`) carry their own `draft_rev`; a redraft ("Ask Claude to
redraft") appends a rev and bumps `current_draft_rev` without touching the resolution.
A redraft that writes no rev says so (`redraft_failed`), since nothing else would.

Assignees come from `routing.apply_route` (the answerer's `route`, validated against
the org's members), falling back to the org's default assignee. Assigning gives each
assignee `routed` thread access (`thread.access_changed` to that member) and sends
`demo.needs_human`; reassigning moves the item and that access, and records nothing
else (§6).

Writes queue an `escalation` snapshot (and, for `add_draft`, an `escalation_draft`
snapshot: append-only, version 1, entity id `draft_entity_id(escalation, rev)`) to the
owning org's thread scope, or its org scope when there is no thread.

Owner: E2.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from pydantic import JsonValue
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents.models import EscalationResolution, EscalationStatus, Route
from harmony.db import repo
from harmony.db.models import (
    Escalation,
    EscalationDraft,
    Member,
    NetworkRequest,
    Org,
    Thread,
    ThreadAccess,
)
from harmony.db.models.choices import EscalationKind, Side
from harmony.db.uow import UnitOfWork
from harmony.domain import conversations, threads
from harmony.domain.errors import EscalationNotOpen, InvalidRoute, NotInOrg
from harmony.events.schemas import (
    DemoNeedsHuman,
    EntitySnapshot,
    EventScope,
    RedraftFailed,
    ThreadAccessChanged,
)

if TYPE_CHECKING:
    from harmony.agents.consult import ConsultDraft

log = logging.getLogger(__name__)

ESCALATION_DRAFT_ENTITY = "escalation_draft"
# Separates a dedupe key from the id that makes a repeat of a settled item unique.
_REPEAT = "#"


def scope_of(escalation: Escalation) -> EventScope:
    """Org-private: the owning org's thread scope, or its org scope."""
    if escalation.thread_id is None:
        return EventScope.org(escalation.org_id)
    return EventScope.thread(escalation.org_id, escalation.thread_id)


def draft_entity_id(escalation_id: UUID, draft_rev: int) -> UUID:
    """The `escalation_draft` snapshot's entity id (drafts are keyed by (escalation, rev))."""
    return uuid5(escalation_id, f"draft/{draft_rev}")


def held_message_id(escalation: Escalation) -> UUID:
    """The id an `outbound_review` item's held message is sent under once approved, so
    a retried approval sends it once."""
    return uuid5(escalation.id, "message")


def assignee_ids(escalation: Escalation) -> list[UUID]:
    return [UUID(str(member_id)) for member_id in escalation.assignees]


async def locked(uow: UnitOfWork, escalation_id: UUID) -> Escalation:
    """The escalation, row-locked: serializes its assignment and drafts."""
    escalation = await uow.session.get(
        Escalation, (uow.world_id, escalation_id), with_for_update=True, populate_existing=True
    )
    if escalation is None:
        raise repo.NotFoundInWorld(Escalation, uow.world_id, escalation_id)
    return escalation


def _not_open(escalation: Escalation) -> EscalationNotOpen:
    return EscalationNotOpen(
        escalation.id,
        escalation.status,
        resolved_by_member_id=escalation.resolved_by_member_id,
        resolved_at=escalation.resolved_at,
    )


async def open(
    uow: UnitOfWork,
    *,
    escalation_id: UUID,
    org_id: UUID,
    kind: EscalationKind,
    dedupe_key: str,
    thread_id: UUID | None = None,
    side: Side | None = None,
    consult_id: UUID | None = None,
) -> tuple[Escalation, bool]:
    """Open an escalation with no assignees yet. Returns `(escalation, created)`;
    `created` is False when an *open* escalation already has `dedupe_key` (a person is
    already deciding it). A resolved or cancelled one doesn't count: the same subject
    held again (a message a member declined, then sent again) is a new item, stored
    under `<dedupe_key>#<escalation_id>` because the key is unique per world.
    Idempotent on `escalation_id`."""
    if kind == "consult" and consult_id is None:
        raise ValueError("a consult escalation needs consult_id")
    session = uow.session
    existing = await repo.get(session, Escalation, uow.world_id, escalation_id)
    if existing is not None:
        return existing, True
    in_world = repo.in_world(Escalation, uow.world_id)
    same = await session.scalar(
        in_world.where(
            Escalation.status == "open",
            or_(
                Escalation.dedupe_key == dedupe_key,
                Escalation.dedupe_key.startswith(f"{dedupe_key}{_REPEAT}", autoescape=True),
            ),
        ).order_by(Escalation.created_at)
    )
    if same is not None:
        return same, False
    taken = await session.scalar(
        select(Escalation.id).where(
            Escalation.world_id == uow.world_id, Escalation.dedupe_key == dedupe_key
        )
    )
    escalation = Escalation(
        world_id=uow.world_id,
        id=escalation_id,
        org_id=org_id,
        thread_id=thread_id,
        side=side,
        kind=kind,
        consult_id=consult_id,
        status="open",
        dedupe_key=dedupe_key if taken is None else f"{dedupe_key}{_REPEAT}{escalation_id}",
        assignees=[],
    )
    session.add(escalation)
    await session.flush()
    uow.add_snapshot(escalation, scope_of(escalation))
    await _refresh_views(uow, escalation)
    return escalation, True


async def add_draft(
    uow: UnitOfWork,
    escalation_id: UUID,
    draft: ConsultDraft,
    *,
    turn_id: UUID | None,
    draft_rev: int | None = None,
) -> EscalationDraft:
    """Append the next `draft_rev` and make it current: the answerer's draft of a
    consult answer, or the held message of an `outbound_review` (its `answer`). With
    `draft_rev` (the rev the answerer drafted), idempotent: an existing rev is returned
    unchanged."""
    session = uow.session
    escalation = await locked(uow, escalation_id)
    if draft_rev is not None:
        existing = await session.get(EscalationDraft, (uow.world_id, escalation_id, draft_rev))
        if existing is not None:
            return existing
        rev = draft_rev
    else:
        last = await session.scalar(
            select(func.max(EscalationDraft.draft_rev)).where(
                EscalationDraft.world_id == uow.world_id,
                EscalationDraft.escalation_id == escalation_id,
            )
        )
        rev = (last or 0) + 1
    row = EscalationDraft(
        world_id=uow.world_id,
        escalation_id=escalation_id,
        draft_rev=rev,
        answer=draft.answer,
        route=draft.route.model_dump(mode="json") if draft.route else None,
        rationale=draft.rationale,
        short_question=draft.short_question,
        turn_id=turn_id,
    )
    session.add(row)
    escalation.current_draft_rev = max(escalation.current_draft_rev or 0, rev)
    await session.flush()
    uow.add_event(
        EntitySnapshot(
            entity_type=ESCALATION_DRAFT_ENTITY,
            entity_id=draft_entity_id(escalation_id, rev),
            version=1,
            data=await _row_data(session, row),
        ),
        scope_of(escalation),
    )
    uow.add_snapshot(escalation, scope_of(escalation))
    return row


async def assign(uow: UnitOfWork, escalation_id: UUID, *, member_ids: Sequence[UUID]) -> Escalation:
    """Set the assignees and give each `routed` thread access. Members dropped from the
    item lose the `routed` access it gave them. Raises `InvalidRoute` for nobody, or for
    anyone outside the escalation's org."""
    session = uow.session
    escalation = await locked(uow, escalation_id)
    wanted = list(dict.fromkeys(member_ids))
    members = await _members(session, uow.world_id, wanted)
    strangers = [m for m in wanted if m not in members or members[m].org_id != escalation.org_id]
    if strangers or not wanted:
        raise InvalidRoute(f"can't assign {escalation_id} to {strangers or 'nobody'}")
    previous = assignee_ids(escalation)
    escalation.assignees = [str(member_id) for member_id in wanted]
    if escalation.thread_id is not None:
        thread = await repo.get_required(session, Thread, uow.world_id, escalation.thread_id)
        for member_id in wanted:
            await threads.add_access(uow, thread, member_id=member_id, org_id=escalation.org_id)
        for member_id in previous:
            if member_id not in wanted:
                await _drop_routed(uow, escalation, member_id)
    await session.flush()
    uow.add_snapshot(escalation, scope_of(escalation))
    await _refresh_views(uow, escalation)
    if escalation.status == "open":
        org = await repo.get_required(session, Org, uow.world_id, escalation.org_id)
        for member_id in wanted:
            if member_id not in previous:
                uow.add_event(
                    DemoNeedsHuman(
                        org_slug=org.slug,
                        member_slug=members[member_id].slug,
                        entity_id=escalation.id,
                    ),
                    EventScope.demo(),
                )
    return escalation


async def resolve(uow: UnitOfWork, resolution: EscalationResolution) -> Escalation:
    """`open` → `resolved`, storing the resolution and its member. Raises
    `EscalationNotOpen` when someone else got there first, and `NotInOrg` when the
    member isn't in the escalation's org. The caller applies the effect (the release or
    the message) in the same unit of work."""
    session = uow.session
    world_id = uow.world_id
    escalation = await repo.get_required(session, Escalation, world_id, resolution.escalation_id)
    if escalation.status != "open":
        raise _not_open(escalation)
    await _check_member(session, escalation, resolution.member_id, claimed_org=resolution.org_id)
    resolved = await session.scalar(
        update(Escalation)
        .where(
            Escalation.world_id == world_id,
            Escalation.id == resolution.escalation_id,
            Escalation.status == "open",
        )
        .values(
            status="resolved",
            resolution=resolution.model_dump(mode="json"),
            resolved_by_member_id=resolution.member_id,
            resolved_at=datetime.now(UTC),
            version=Escalation.version + 1,
        )
        .returning(Escalation.id)
        .execution_options(synchronize_session=False)
    )
    escalation = await _reloaded(session, world_id, resolution.escalation_id)
    if resolved is None:
        raise _not_open(escalation)
    uow.add_snapshot(escalation, scope_of(escalation))
    await _refresh_views(uow, escalation)
    return escalation


async def reassign(
    uow: UnitOfWork,
    escalation_id: UUID,
    *,
    to_member_ids: Sequence[UUID],
    member_id: UUID,
) -> Escalation:
    """A member moves an open escalation, and the thread access it gives, to other
    members of its org. A plain move: nothing is recorded for routing to learn from
    (§6). Moving it to its current assignees changes nothing, so a retry is a no-op."""
    escalation = await locked(uow, escalation_id)
    if escalation.status != "open":
        raise _not_open(escalation)
    await _check_member(uow.session, escalation, member_id)
    if set(to_member_ids) == set(assignee_ids(escalation)):
        return escalation
    return await assign(uow, escalation_id, member_ids=to_member_ids)


async def cancel(uow: UnitOfWork, escalation_id: UUID, *, reason: str) -> Escalation:
    """`open` → `cancelled` (the thread ended or the consult was withdrawn). A resolved
    or cancelled escalation is returned unchanged."""
    session = uow.session
    cancelled = await session.scalar(
        update(Escalation)
        .where(
            Escalation.world_id == uow.world_id,
            Escalation.id == escalation_id,
            Escalation.status == "open",
        )
        .values(status="cancelled", resolved_at=datetime.now(UTC), version=Escalation.version + 1)
        .returning(Escalation.id)
        .execution_options(synchronize_session=False)
    )
    escalation = await _reloaded(session, uow.world_id, escalation_id)
    if cancelled is not None:
        log.info("escalation %s cancelled: %s", escalation_id, reason)
        uow.add_snapshot(escalation, scope_of(escalation))
        await _refresh_views(uow, escalation)
    return escalation


async def redraft_failed(uow: UnitOfWork, escalation_id: UUID, draft_rev: int) -> None:
    """Tell the members who see the item that redraft `draft_rev` wrote nothing, so
    they stop waiting for it (`RedraftFailed`). Nobody waits on a resolved or cancelled
    item."""
    escalation = await repo.get_required(uow.session, Escalation, uow.world_id, escalation_id)
    if escalation.status != "open":
        return
    uow.add_event(
        RedraftFailed(escalation_id=escalation_id, draft_rev=draft_rev), scope_of(escalation)
    )


def escalation_status(value: str) -> EscalationStatus:
    if value == "open" or value == "resolved" or value == "cancelled":
        return value
    raise ValueError(f"unknown escalation status {value!r}")


async def set_status(uow: UnitOfWork, escalation_id: UUID, status: EscalationStatus) -> Escalation:
    escalation = await locked(uow, escalation_id)
    if escalation.status != status:
        escalation.status = status
        await uow.session.flush()
        uow.add_snapshot(escalation, scope_of(escalation))
    return escalation


async def drafts(
    session: AsyncSession, *, world_id: UUID, escalation_id: UUID
) -> list[EscalationDraft]:
    """All drafts, oldest rev first."""
    query = (
        repo.in_world(EscalationDraft, world_id)
        .where(EscalationDraft.escalation_id == escalation_id)
        .order_by(EscalationDraft.draft_rev)
    )
    return list(await session.scalars(query))


async def resolved_draft(
    session: AsyncSession, escalation: Escalation, resolution: EscalationResolution
) -> EscalationDraft | None:
    """The draft rev the member resolved (`draft_rev`, else the current one): a newer
    rev never replaces the one they reviewed (§7)."""
    rev = resolution.draft_rev or escalation.current_draft_rev
    if rev is None:
        return None
    draft = await session.get(EscalationDraft, (escalation.world_id, escalation.id, rev))
    if draft is None:
        raise ValueError(f"escalation {escalation.id} has no draft rev {rev}")
    return draft


async def current_draft(session: AsyncSession, escalation: Escalation) -> EscalationDraft | None:
    if escalation.current_draft_rev is None:
        return None
    key = (escalation.world_id, escalation.id, escalation.current_draft_rev)
    return await session.get(EscalationDraft, key)


def route_of(draft: EscalationDraft) -> Route | None:
    return Route.model_validate(draft.route) if draft.route is not None else None


# --- Internals ----------------------------------------------------------------------


async def _check_member(
    session: AsyncSession,
    escalation: Escalation,
    member_id: UUID,
    *,
    claimed_org: UUID | None = None,
) -> None:
    """Only a member of the escalation's org may act on it, whatever org they claim."""
    member = await repo.get(session, Member, escalation.world_id, member_id)
    in_org = member is not None and member.org_id == escalation.org_id
    if not in_org or claimed_org not in (None, escalation.org_id):
        raise NotInOrg(member_id, escalation.org_id)


async def _members(
    session: AsyncSession, world_id: UUID, member_ids: Sequence[UUID]
) -> dict[UUID, Member]:
    if not member_ids:
        return {}
    rows = await repo.list_in_world(session, Member, world_id, Member.id.in_(member_ids))
    return {row.id: row for row in rows}


async def _drop_routed(uow: UnitOfWork, escalation: Escalation, member_id: UUID) -> None:
    """Remove the `routed` access this item gave, unless the member is routed to the
    thread another way: another item on it, open or settled, that was theirs or that
    they resolved (they keep following the conversation, `inbox`), or the request they
    sent."""
    session = uow.session
    thread_id = escalation.thread_id
    assert thread_id is not None
    other_items = await session.scalars(
        repo.in_world(Escalation, uow.world_id).where(
            Escalation.thread_id == thread_id,
            Escalation.org_id == escalation.org_id,
            Escalation.id != escalation.id,
        )
    )
    if any(
        member_id in assignee_ids(item) or item.resolved_by_member_id == member_id
        for item in other_items
    ):
        return
    creator = await session.scalar(
        select(NetworkRequest.created_by_member_id)
        .join(
            Thread,
            (Thread.world_id == NetworkRequest.world_id) & (Thread.request_id == NetworkRequest.id),
        )
        .where(Thread.world_id == uow.world_id, Thread.id == thread_id)
    )
    if creator == member_id:
        return
    removed = await session.scalar(
        delete(ThreadAccess)
        .where(
            ThreadAccess.world_id == uow.world_id,
            ThreadAccess.thread_id == thread_id,
            ThreadAccess.member_id == member_id,
            ThreadAccess.reason == "routed",
        )
        .returning(ThreadAccess.member_id)
        .execution_options(synchronize_session=False)
    )
    if removed is not None:
        uow.add_event(
            ThreadAccessChanged(
                thread_id=thread_id, member_id=member_id, reason="routed", added=False
            ),
            EventScope.member(member_id, escalation.org_id),
        )


async def _refresh_views(uow: UnitOfWork, escalation: Escalation) -> None:
    """An item opened, moved or settled: refresh the views built from it that its own
    org-private snapshot doesn't reach. Both parties get the thread's snapshot, since
    whether the thread waits on a person shows in the other side's request status; and
    a requester-side consult shows inline in the request's chat, which refetches."""
    if escalation.thread_id is None:
        return
    session = uow.session
    thread = await repo.get_required(session, Thread, uow.world_id, escalation.thread_id)
    threads.snapshot(uow, thread)
    if escalation.kind != "consult" or threads.side_of(thread, escalation.org_id) != "requester":
        return
    request = await repo.get(session, NetworkRequest, uow.world_id, thread.request_id)
    if request is not None and request.conversation_id is not None:
        await conversations.touch(uow, request.conversation_id)


async def _reloaded(session: AsyncSession, world_id: UUID, escalation_id: UUID) -> Escalation:
    escalation = await session.get(Escalation, (world_id, escalation_id), populate_existing=True)
    if escalation is None:
        raise repo.NotFoundInWorld(Escalation, world_id, escalation_id)
    return escalation


async def _row_data(session: AsyncSession, row: EscalationDraft) -> dict[str, JsonValue]:
    if "created_at" in sa_inspect(row).unloaded:
        await session.refresh(row, attribute_names=["created_at"])
    return threads.row_data(row)


__all__ = [
    "ESCALATION_DRAFT_ENTITY",
    "add_draft",
    "assign",
    "assignee_ids",
    "cancel",
    "current_draft",
    "draft_entity_id",
    "drafts",
    "escalation_status",
    "held_message_id",
    "locked",
    "open",
    "reassign",
    "redraft_failed",
    "resolve",
    "resolved_draft",
    "route_of",
    "scope_of",
    "set_status",
]
