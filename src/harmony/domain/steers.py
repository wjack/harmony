"""Steers: a member's instruction to its side's Claude on a thread, and the guidance
written from it (docs/design/EDIT.md §2, §3).

A steer is an Edit (`kind = edit`) or the text a Reopen starts with (`reopen`). Its id
is minted here from (world, org, member, the client's key), so a retried POST finds the
same row, and two members, orgs or worlds never share one. `record` stores it under the
thread lock at the next per-(thread, side) `seq`: the steer's cassette turn, and the
watermark the representative's guard compares against (`max_seq`, EDIT.md §3.7).

Guidance (`rep_guidance`) is what one side's representative follows, append-only per
(thread, side): `latest_guidance` reads the newest version, `add_guidance` writes the
next. The empty string means the org withdrew it. Guidance must keep to members' words:
`added_details` is the code check that rejects a number, date, email, link or name none
of them typed.

`settle` decides a steer's status from the rows its parts wrote, never from what the
workflow believes, and only once: it changes only a `working` row.

Steers and guidance are org-private: their snapshots go to `EventScope.thread(org,
thread)` only, the steer's without `draft_text` (`Steer.snapshot_omits`).

Owner: E4.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents.context import quoted
from harmony.db import repo
from harmony.db.models import Escalation, EscalationDraft, Member, Org, RepGuidance, Steer
from harmony.db.models.choices import (
    Side,
    SteerDraftOutcome,
    SteerFailure,
    SteerGuidanceOutcome,
    SteerKind,
)
from harmony.db.uow import UnitOfWork
from harmony.domain import threads
from harmony.domain.errors import SteerConflict
from harmony.events.schemas import EntitySnapshot, EventScope
from harmony.names import name_parts

STEER_NAMESPACE: Final = uuid5(NAMESPACE_URL, "https://harmony.invalid/steers")
GUIDANCE_MAX_CHARS: Final = 2000
REP_GUIDANCE_ENTITY: Final = "rep_guidance"

_NUMBER: Final = re.compile(r"\d+(?:[.,]\d+)*")
_MONTH: Final = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_DAY: Final = r"(\d{1,2})(?:st|nd|rd|th)?"
# A month next to a day number, either way round ("Nov 12", "12 November"). A month on
# its own isn't a date, so "May" as a verb never counts.
_MONTH_DAY: Final = re.compile(rf"\b{_MONTH}\s+{_DAY}\b", re.IGNORECASE)
_DAY_MONTH: Final = re.compile(rf"\b{_DAY}\s+(?:of\s+)?{_MONTH}\b", re.IGNORECASE)
_WEEKDAY: Final = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b", re.IGNORECASE
)
_EMAIL: Final = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL: Final = re.compile(r"\b(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
_URL_TRAILING: Final = ".,;:!?)]}"


def steer_id_for(world_id: UUID, org_id: UUID, member_id: UUID, client_key: UUID) -> UUID:
    """The steer a member's submit names: one per (world, org, member, client key)."""
    return uuid5(STEER_NAMESPACE, f"{world_id}/{org_id}/{member_id}/{client_key}")


def guidance_id(steer_id: UUID) -> UUID:
    """The `rep_guidance` row a steer writes, so a retried write finds its own row."""
    return uuid5(steer_id, "rep_guidance")


async def record(
    uow: UnitOfWork,
    *,
    steer_id: UUID,
    thread_id: UUID,
    org_id: UUID,
    kind: SteerKind,
    author_member_id: UUID,
    body: str,
    escalation_id: UUID | None = None,
    draft_text: str | None = None,
    draft_rev: int | None = None,
) -> Steer:
    """Store the steer at its side's next `seq`, under the thread lock. Idempotent: the
    same id with the same thread, org, author and body is the stored row; with anything
    else, `SteerConflict`. Raises `ValueError` for an org that isn't a party."""
    session = uow.session
    thread = await threads.locked(uow, thread_id)
    side = threads.side_of(thread, org_id)
    existing = await repo.get(session, Steer, uow.world_id, steer_id)
    if existing is not None:
        stored = (existing.thread_id, existing.org_id, existing.author_member_id, existing.body)
        if stored != (thread_id, org_id, author_member_id, body):
            raise SteerConflict(steer_id)
        return existing
    steer = Steer(
        world_id=uow.world_id,
        id=steer_id,
        thread_id=thread_id,
        org_id=org_id,
        side=side,
        seq=await max_seq(session, uow.world_id, thread_id, side) + 1,
        kind=kind,
        author_member_id=author_member_id,
        body=body,
        escalation_id=escalation_id,
        draft_text=draft_text,
        draft_rev=draft_rev,
    )
    session.add(steer)
    await session.flush()
    uow.add_snapshot(steer, _scope(steer))
    return steer


async def locked(uow: UnitOfWork, steer_id: UUID) -> Steer:
    """The steer, after its thread's row lock: every writer of a steer takes that lock
    first, so its versioned updates never race."""
    steer = await repo.get_required(uow.session, Steer, uow.world_id, steer_id)
    await threads.locked(uow, steer.thread_id)
    await uow.session.refresh(steer)
    return steer


async def max_seq(session: AsyncSession, world_id: UUID, thread_id: UUID, side: Side) -> int:
    """The newest steer's `seq` for (thread, side), whatever its status; 0 for none."""
    last = await session.scalar(
        select(func.max(Steer.seq)).where(
            Steer.world_id == world_id, Steer.thread_id == thread_id, Steer.side == side
        )
    )
    return last or 0


async def for_thread(
    session: AsyncSession, world_id: UUID, thread_id: UUID, org_id: UUID
) -> list[Steer]:
    """One org's steers on a thread, oldest first."""
    rows = await session.scalars(
        repo.in_world(Steer, world_id)
        .where(Steer.thread_id == thread_id, Steer.org_id == org_id)
        .order_by(Steer.seq)
    )
    return list(rows)


# --- Guidance -------------------------------------------------------------------------


async def latest_guidance(
    session: AsyncSession, world_id: UUID, thread_id: UUID, side: Side
) -> RepGuidance | None:
    """The guidance (thread, side) follows now, or None before any."""
    return await session.scalar(
        repo.in_world(RepGuidance, world_id)
        .where(RepGuidance.thread_id == thread_id, RepGuidance.side == side)
        .order_by(RepGuidance.version.desc())
        .limit(1)
    )


async def guidance_version(
    session: AsyncSession, world_id: UUID, thread_id: UUID, side: Side, version: int
) -> RepGuidance | None:
    return await session.scalar(
        repo.in_world(RepGuidance, world_id).where(
            RepGuidance.thread_id == thread_id,
            RepGuidance.side == side,
            RepGuidance.version == version,
        )
    )


async def current_guidance_quoted(
    session: AsyncSession,
    world_id: UUID,
    thread_id: UUID,
    side: Side,
    *,
    org_name: str,
    empty: str = "None.",
) -> str:
    """The side's current guidance as quoted data from its org, or `empty` when there
    is none (never written, or withdrawn)."""
    guidance = await latest_guidance(session, world_id, thread_id, side)
    if guidance is None or not guidance.text.strip():
        return empty
    return quoted("guidance", guidance.text, from_=org_name)


async def add_guidance(uow: UnitOfWork, steer: Steer, text: str) -> RepGuidance:
    """Append `text` as the side's next guidance version, from `steer`. The caller holds
    the thread lock. Idempotent: the steer's own row, if it wrote one already."""
    session = uow.session
    existing = await repo.get(session, RepGuidance, uow.world_id, guidance_id(steer.id))
    if existing is not None:
        return existing
    if len(text) > GUIDANCE_MAX_CHARS:
        raise ValueError(f"guidance is at most {GUIDANCE_MAX_CHARS} characters")
    last = await latest_guidance(session, uow.world_id, steer.thread_id, steer.side)
    row = RepGuidance(
        world_id=uow.world_id,
        id=guidance_id(steer.id),
        thread_id=steer.thread_id,
        org_id=steer.org_id,
        side=steer.side,
        version=(last.version if last is not None else 0) + 1,
        text=text,
        steer_id=steer.id,
        created_by_member_id=steer.author_member_id,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row, attribute_names=["created_at"])
    uow.add_event(
        EntitySnapshot(
            entity_type=REP_GUIDANCE_ENTITY,
            entity_id=row.id,
            version=1,
            data=threads.row_data(row),
        ),
        _scope(steer),
    )
    return row


async def set_guidance_outcome(
    uow: UnitOfWork,
    steer_id: UUID,
    *,
    outcome: SteerGuidanceOutcome,
    text: str | None = None,
    failure: SteerFailure | None = None,
) -> Steer:
    """Store what the steer's guidance call came to: on `updated`, `text` as the side's
    next guidance version; on `rejected`, why. Only once: a steer whose guidance
    outcome is already stored (a retry, or a settle that came first) is returned as it
    is."""
    steer = await locked(uow, steer_id)
    if steer.guidance_outcome is not None:
        return steer
    if outcome == "updated":
        if text is None:
            raise ValueError("updated guidance needs its text")
        steer.guidance_version = (await add_guidance(uow, steer, text)).version
    steer.guidance_outcome = outcome
    if outcome == "rejected":
        steer.failure_reason = failure or "invalid"
    snapshot(uow, steer)
    return steer


def snapshot(uow: UnitOfWork, steer: Steer) -> None:
    """Queue the steer's snapshot to its org's thread scope."""
    uow.add_snapshot(steer, _scope(steer))


# --- Settling -------------------------------------------------------------------------


async def settle(uow: UnitOfWork, steer_id: UUID, *, failure: SteerFailure | None) -> Steer | None:
    """Decide a `working` steer's outcome from the rows its parts wrote (EDIT.md §3.2):
    guidance not written becomes `failed`; the draft is `rewritten` when its rev exists,
    `settled` when a member resolved the item first, else `failed`. `failure` is the
    workflow's reason for a part that didn't land (`thread_closed` when the thread
    ended). A reopen steer is `done` only through the reopen itself, so here it always
    fails (EDIT.md §3.9). A settled steer is left as it is."""
    if await repo.get(uow.session, Steer, uow.world_id, steer_id) is None:
        return None
    steer = await locked(uow, steer_id)
    if steer.status != "working":
        return steer
    rejected = steer.failure_reason if steer.guidance_outcome == "rejected" else None
    if steer.guidance_outcome is None:
        steer.guidance_outcome = "failed"
    if steer.kind == "reopen":
        unchanged: SteerFailure | None = (
            "unchanged" if steer.guidance_outcome == "unchanged" else None
        )
        steer.status = "failed"
        steer.failure_reason = failure or rejected or unchanged or "reopen_failed"
        snapshot(uow, steer)
        return steer
    if steer.escalation_id is not None:
        steer.draft_outcome = await _draft_outcome(
            uow.session, uow.world_id, steer.escalation_id, steer.draft_rev
        )
    landed = steer.guidance_outcome in ("updated", "unchanged") and steer.draft_outcome in (
        None,
        "rewritten",
        "settled",
    )
    steer.status = "done" if landed else "failed"
    if not landed:
        steer.failure_reason = rejected or failure or "model_error"
    snapshot(uow, steer)
    return steer


async def _draft_outcome(
    session: AsyncSession, world_id: UUID, escalation_id: UUID, draft_rev: int | None
) -> SteerDraftOutcome | None:
    """`rewritten` when the reserved rev exists; `settled` when a member resolved the
    item first; else `failed` (the rewrite failed, or the thread ended first). With no
    rev reserved the item wasn't rewritten: `settled` if it was resolved, else none."""
    if (
        draft_rev is not None
        and await session.get(EscalationDraft, (world_id, escalation_id, draft_rev)) is not None
    ):
        return "rewritten"
    item = await repo.get(session, Escalation, world_id, escalation_id)
    if item is not None and item.status == "resolved":
        return "settled"
    return "failed" if draft_rev is not None else None


# --- The fact check -------------------------------------------------------------------


async def world_names(session: AsyncSession, world_id: UUID) -> list[str]:
    """The names guidance may use only when a member typed them (`checked_names`), for
    every org and member in the world."""
    orgs = await session.scalars(select(Org.name).where(Org.world_id == world_id))
    members = await session.scalars(select(Member.name).where(Member.world_id == world_id))
    return checked_names(orgs, members)


def checked_names(orgs: Iterable[str], members: Iterable[str]) -> list[str]:
    """Every org's name, and every member's full name, name without its title and first
    name ("Dr. Helen Ward", "Helen Ward", "Helen"). A title alone is no one's name."""
    names: list[str] = [" ".join(name.split()) for name in orgs]
    for name in members:
        if untitled := name_parts(name):
            names.extend((" ".join(name.split()), " ".join(untitled), untitled[0]))
    return [name for name in dict.fromkeys(names) if name]


def added_details(text: str, *, allowed: Sequence[str], names: Iterable[str]) -> list[str]:
    """What `text` states that no `allowed` text does: numbers (commas ignored), dates
    (a month next to a day number, or a weekday), emails, links, and any of `names`
    (whole words, any case). Empty when it adds nothing. Spelled-out numbers, product
    names and paraphrase get through: it is a cheap check, not a judge (EDIT.md §6)."""
    found: list[str] = []
    for tokens in (_numbers, _dates, _weekdays, _emails, _urls):
        known = {token for source in allowed for token in tokens(source)}
        found.extend(token for token in tokens(text) if token not in known)
    for name in names:
        pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.IGNORECASE)
        if pattern.search(text) and not any(pattern.search(source) for source in allowed):
            found.append(name)
    return list(dict.fromkeys(found))


def _numbers(text: str) -> list[str]:
    return [match.replace(",", "") for match in _NUMBER.findall(text)]


def _dates(text: str) -> list[str]:
    month_first = [(month, day) for month, day in _MONTH_DAY.findall(text)]
    day_first = [(month, day) for day, month in _DAY_MONTH.findall(text)]
    return [f"{month[:3].lower()} {int(day)}" for month, day in month_first + day_first]


def _weekdays(text: str) -> list[str]:
    return [day.lower() for day in _WEEKDAY.findall(text)]


def _emails(text: str) -> list[str]:
    return [email.lower() for email in _EMAIL.findall(text)]


def _urls(text: str) -> list[str]:
    return [url.rstrip(_URL_TRAILING).lower() for url in _URL.findall(text)]


def _scope(steer: Steer | RepGuidance) -> EventScope:
    return EventScope.thread(steer.org_id, steer.thread_id)


__all__ = [
    "GUIDANCE_MAX_CHARS",
    "REP_GUIDANCE_ENTITY",
    "STEER_NAMESPACE",
    "add_guidance",
    "added_details",
    "checked_names",
    "current_guidance_quoted",
    "for_thread",
    "guidance_id",
    "guidance_version",
    "latest_guidance",
    "locked",
    "max_seq",
    "record",
    "set_guidance_outcome",
    "settle",
    "snapshot",
    "steer_id_for",
    "world_names",
]
