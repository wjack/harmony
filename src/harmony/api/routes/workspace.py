"""Thread workspace routes: one side's private view of a thread (§8, §10,
UI_REFERENCE §6).

- GET /api/threads/{thread_id}/workspace: the acting member's org's side panel
- POST /api/threads/{thread_id}/notes {body}: add an internal note

P0's `GET /api/threads/{thread_id}` keeps returning the shared thread (messages both
sides see). The workspace adds what only the acting member's side may see: its consults
(drafts, sharing checks, releases), its escalations with each draft and what internal
Claude looked at, its internal notes, its own close summary, and "What our
representative saw": the parts of the side's frozen system prompt plus every answer
released to it (REVISION_UNSTRUCTURED.md §2). Same access rule as the thread (404,
never 403). Carries `X-Harmony-Cursor`; `consult`, `escalation`, `escalation_draft` and
`internal_note` snapshots keep it live.

Owner: E4.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents import consult as answerer
from harmony.agents import context, keys, release
from harmony.agents.consult import SharingCheck
from harmony.agents.models import (
    ActivityLine,
    ContextPart,
    EscalationResolution,
    EscalationStatus,
    MemberRef,
    Route,
)
from harmony.api.deps import SessionmakerDep, read_cursor, set_cursor, snapshot_session
from harmony.api.routes.inbox import HELD_MESSAGE_REASON
from harmony.api.routes.problems import invalid, not_found, problems, world_retired
from harmony.auth import access
from harmony.auth.session import Actor, ActorDep
from harmony.db import repo
from harmony.db.models import (
    Consult,
    Escalation,
    EscalationDraft,
    InternalNote,
    Member,
    RepSession,
    Thread,
)
from harmony.db.models.base import JsonObject
from harmony.db.models.choices import (
    ConsultStatus,
    EscalationKind,
    NoteAuthor,
    ReleasedBy,
    Side,
)
from harmony.db.uow import unit_of_work
from harmony.domain import consults, escalations, notes, routing, threads
from harmony.domain.notes import BODY_MAX_CHARS
from harmony.errors import WorldRetired

router = APIRouter(tags=["threads"])


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class ConsultView(_View):
    id: UUID
    question: str
    status: ConsultStatus
    # The answerer's newest draft and its verdicts; None until one is written.
    answer: str | None
    needs_human: bool | None
    needs_human_reason: str | None
    # None when no check ran (*Ask*, *Skip*, or a draft that needed a person anyway).
    sharing_check: SharingCheck | None
    # What the representative received, once released, and who released it.
    released_text: str | None
    released_by: ReleasedBy | None
    released_by_member: MemberRef | None
    created_at: datetime
    released_at: datetime | None
    version: int


class RouteAssigneeView(_View):
    slug: str
    # Why it is theirs, addressed to them ("you run XRD and SEM").
    why: str


class RouteView(_View):
    """A draft's route as it was applied: the members it named, each with why."""

    assignees: list[RouteAssigneeView]


class EscalationDraftView(_View):
    draft_rev: int
    # The drafted answer (a consult), or the held message (`outbound_review`).
    answer: str
    # The decision in one line, for the reviewer; None on older drafts.
    short_question: str | None = None
    route: RouteView | None
    rationale: str
    # What internal Claude looked at while drafting, from its tool calls.
    activity: list[ActivityLine]
    created_at: datetime


class EscalationView(_View):
    id: UUID
    kind: EscalationKind
    status: EscalationStatus
    consult_id: UUID | None
    # "Why this needs you" (`release.why_needed`, or the held-message reason).
    reason: str
    assignees: list[MemberRef]
    current_draft_rev: int | None
    drafts: list[EscalationDraftView]
    resolution: EscalationResolution | None
    resolved_by: MemberRef | None
    resolved_at: datetime | None
    created_at: datetime
    version: int


class NoteView(_View):
    id: UUID
    seq: int
    author_kind: NoteAuthor
    author: MemberRef | None
    body: str
    mentions: list[MemberRef]
    created_at: datetime


class ReleasedAnswerView(_View):
    """What its side told the representative: a settled `consult` call's result, or a
    held message's result that quotes what a person wrote (an edit, their own message,
    or what it may say instead)."""

    # The consult, or the held message's escalation.
    id: UUID
    # The consult's question; None for a held message.
    question: str | None
    text: str
    released_by: ReleasedBy
    released_by_member: MemberRef | None
    released_at: datetime


class RepresentativeContext(_View):
    """ "What our representative saw": the parts its requests carry and the answers
    released to it (§2): its frozen prompt, the notices sent since (R§12), and the
    released results. A send-back's note isn't a released answer, so it isn't listed."""

    parts: list[ContextPart]
    notices: list[context.DeliveredNotice]
    # Only those a stored transcript row carries: a thread can end before one is given.
    released: list[ReleasedAnswerView]
    # When the system prompt was frozen: this side's first turn.
    frozen_at: datetime


class ThreadWorkspace(_View):
    thread_id: UUID
    # The acting member's side; everything below is that side's.
    side: Side
    consults: list[ConsultView]
    escalations: list[EscalationView]
    notes: list[NoteView]
    # None until this side's representative has taken its first turn.
    representative: RepresentativeContext | None
    # This side's own close summary, once written; the other side's is never shown.
    close_summary: str | None
    # The thread's version this view is consistent with.
    version: int


class AddNote(BaseModel):
    """`@slug` mentions in `body` share the thread with those teammates."""

    body: str = Field(min_length=1, max_length=BODY_MAX_CHARS)


@router.get(
    "/threads/{thread_id}/workspace",
    response_model=ThreadWorkspace,
    responses=problems(status.HTTP_404_NOT_FOUND, cursor=True),
)
async def get_workspace(
    thread_id: UUID, actor: ActorDep, sessionmaker: SessionmakerDep, response: Response
) -> ThreadWorkspace:
    async with snapshot_session(sessionmaker) as db:
        thread = await _visible(db, actor, thread_id)
        workspace = await _workspace(db, actor, thread)
        cursor = await read_cursor(db, actor.world_id)
    set_cursor(response, cursor)
    return workspace


@router.post(
    "/threads/{thread_id}/notes",
    response_model=NoteView,
    responses=problems(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
)
async def add_note(
    thread_id: UUID, body: AddNote, actor: ActorDep, sessionmaker: SessionmakerDep
) -> NoteView:
    async with sessionmaker() as db:
        await _visible(db, actor, thread_id)
    try:
        async with unit_of_work(sessionmaker, actor.world_id) as uow:
            note = await notes.add(
                uow,
                note_id=uuid4(),
                thread_id=thread_id,
                org_id=actor.org_id,
                author_kind="member",
                author_member_id=actor.member_id,
                body=body.body,
                mentions=notes.parse_mentions(body.body),
            )
            await uow.commit()
    except WorldRetired:
        raise world_retired() from None
    except ValueError as error:
        raise invalid(str(error)) from None
    async with sessionmaker() as db:
        members = await _members(db, actor.world_id, [note.author_member_id])
        mentioned = await _mentioned(db, actor.world_id, actor.org_id, [note])
    return _note_view(note, members, mentioned)


async def _visible(db: AsyncSession, actor: Actor, thread_id: UUID) -> Thread:
    try:
        return await access.get_visible_thread(db, actor, thread_id)
    except repo.NotFoundInWorld:
        raise not_found("thread") from None


async def _workspace(db: AsyncSession, actor: Actor, thread: Thread) -> ThreadWorkspace:
    world_id = actor.world_id
    side = threads.side_of(thread, actor.org_id)
    consult_rows = await consults.for_thread(db, world_id=world_id, thread_id=thread.id, side=side)
    escalation_rows = list(
        await db.scalars(
            repo.in_world(Escalation, world_id)
            .where(Escalation.thread_id == thread.id, Escalation.org_id == actor.org_id)
            .order_by(Escalation.created_at, Escalation.id)
        )
    )
    draft_rows = list(
        await db.scalars(
            repo.in_world(EscalationDraft, world_id)
            .where(EscalationDraft.escalation_id.in_([row.id for row in escalation_rows]))
            .order_by(EscalationDraft.draft_rev)
        )
    )
    # What internal Claude looked at, per consult draft rev; a held message has none.
    consult_escalations = {row.id for row in escalation_rows if row.consult_id is not None}
    activity = {
        (draft.escalation_id, draft.draft_rev): await consults.activity(
            db,
            world_id=world_id,
            org_id=actor.org_id,
            turn_id=draft.turn_id,
            steps=keys.answerer_steps(draft.draft_rev),
        )
        for draft in draft_rows
        if draft.turn_id is not None and draft.escalation_id in consult_escalations
    }
    note_rows = await notes.list_for(
        db, world_id=world_id, thread_id=thread.id, org_id=actor.org_id
    )
    rep = await db.get(RepSession, (world_id, thread.id, side))
    members = await _members(
        db,
        world_id,
        [
            *(m for row in escalation_rows for m in _ids(row.assignees)),
            *(row.resolved_by_member_id for row in escalation_rows),
            *(row.released_by_member_id for row in consult_rows),
            *(note.author_member_id for note in note_rows),
        ],
    )
    mentioned = await _mentioned(db, world_id, actor.org_id, note_rows)
    org_slugs = list(
        await db.scalars(
            select(Member.slug).where(Member.world_id == world_id, Member.org_id == actor.org_id)
        )
    )
    by_consult = {row.id: row for row in consult_rows}
    return ThreadWorkspace(
        thread_id=thread.id,
        side=side,
        consults=[_consult_view(row, members) for row in consult_rows],
        escalations=[
            _escalation_view(
                row,
                by_consult.get(row.consult_id) if row.consult_id is not None else None,
                [d for d in draft_rows if d.escalation_id == row.id],
                activity,
                members,
                org_slugs,
            )
            for row in escalation_rows
        ],
        notes=[_note_view(note, members, mentioned) for note in note_rows],
        representative=(
            RepresentativeContext(
                parts=context.context_parts(rep.system_text),
                notices=await context.delivered_notices(db, world_id, thread.id, side),
                released=_released(
                    await context.received_results(db, world_id, thread.id, side),
                    consult_rows,
                    escalation_rows,
                    draft_rows,
                    members,
                ),
                frozen_at=rep.created_at,
            )
            if rep is not None
            else None
        ),
        close_summary=rep.close_summary if rep is not None else None,
        version=thread.version,
    )


def _consult_view(row: Consult, members: Mapping[UUID, MemberRef]) -> ConsultView:
    return ConsultView(
        id=row.id,
        question=row.question,
        status=row.status,
        answer=row.answer,
        needs_human=row.needs_human,
        needs_human_reason=row.needs_human_reason,
        sharing_check=(
            SharingCheck.model_validate(row.sharing_check)
            if row.sharing_check is not None
            else None
        ),
        released_text=row.released_text,
        released_by=row.released_by,
        released_by_member=_ref(row.released_by_member_id, members),
        created_at=row.created_at,
        released_at=row.released_at,
        version=row.version,
    )


def _released(
    received: set[str],
    consult_rows: Iterable[Consult],
    escalation_rows: Iterable[Escalation],
    draft_rows: Iterable[EscalationDraft],
    members: Mapping[UUID, MemberRef],
) -> list[ReleasedAnswerView]:
    """The released results the representative was given: each consult's, and each
    held message's that releases what a person wrote (a send-back's note doesn't)."""
    released = [
        ReleasedAnswerView(
            id=row.id,
            question=row.question,
            text=row.released_text,
            released_by=row.released_by,
            released_by_member=_ref(row.released_by_member_id, members),
            released_at=row.released_at,
        )
        for row in consult_rows
        if row.released_text in received
        and row.released_by is not None
        and row.released_at is not None
    ]
    drafts = {(d.escalation_id, d.draft_rev): d for d in draft_rows}
    for row in escalation_rows:
        if row.kind != "outbound_review" or row.resolution is None or row.resolved_at is None:
            continue
        resolution = EscalationResolution.model_validate(row.resolution)
        draft = drafts.get((row.id, row.current_draft_rev or 0))
        resolver = _ref(row.resolved_by_member_id, members)
        # Rebuilt exactly as the representative got it (`network._held_outcome`).
        text = answerer.held_result(
            resolution.action,
            text=resolution.text or "",
            drafted=draft.answer if draft is not None else "",
            member_name=resolver.name if resolver is not None else None,
        )
        if context.RELEASED_OPEN in text and text in received:
            released.append(
                ReleasedAnswerView(
                    id=row.id,
                    question=None,
                    text=text,
                    released_by="member",
                    released_by_member=resolver,
                    released_at=row.resolved_at,
                )
            )
    return released


def _escalation_view(
    row: Escalation,
    consult: Consult | None,
    drafts: list[EscalationDraft],
    activity: Mapping[tuple[UUID, int], list[ActivityLine]],
    members: Mapping[UUID, MemberRef],
    org_slugs: Sequence[str],
) -> EscalationView:
    return EscalationView(
        id=row.id,
        kind=row.kind,
        status=escalations.escalation_status(row.status),
        consult_id=row.consult_id,
        reason=release.why_needed(consult) if consult is not None else HELD_MESSAGE_REASON,
        assignees=_refs(row.assignees, members),
        current_draft_rev=row.current_draft_rev,
        drafts=[
            EscalationDraftView(
                draft_rev=draft.draft_rev,
                answer=draft.answer,
                short_question=draft.short_question,
                route=_route_view(draft.route, org_slugs),
                rationale=draft.rationale,
                activity=activity.get((row.id, draft.draft_rev), []),
                created_at=draft.created_at,
            )
            for draft in drafts
        ],
        resolution=(
            EscalationResolution.model_validate(row.resolution)
            if row.resolution is not None
            else None
        ),
        resolved_by=_ref(row.resolved_by_member_id, members),
        resolved_at=row.resolved_at,
        created_at=row.created_at,
        version=row.version,
    )


def _route_view(stored: JsonObject | None, org_slugs: Sequence[str]) -> RouteView | None:
    """A draft's route as the members it named (`routing.named_slugs`, as it was
    applied), so the client can tell whether the item is still with them."""
    if stored is None:
        return None
    named = routing.named_slugs(Route.model_validate(stored).assignees, org_slugs)
    return RouteView(
        assignees=[RouteAssigneeView(slug=slug, why=why) for slug, why in named.items()]
    )


def _note_view(
    note: InternalNote,
    members: Mapping[UUID, MemberRef],
    mentioned: Mapping[str, MemberRef],
) -> NoteView:
    return NoteView(
        id=note.id,
        seq=note.seq,
        author_kind=note.author_kind,
        author=_ref(note.author_member_id, members),
        body=note.body,
        mentions=[mentioned[str(slug)] for slug in note.mentions if str(slug) in mentioned],
        created_at=note.created_at,
    )


async def _members(
    db: AsyncSession, world_id: UUID, member_ids: Iterable[UUID | None]
) -> dict[UUID, MemberRef]:
    wanted = {member_id for member_id in member_ids if member_id is not None}
    if not wanted:
        return {}
    rows = await db.scalars(
        select(Member).where(Member.world_id == world_id, Member.id.in_(wanted))
    )
    return {m.id: MemberRef(id=m.id, slug=m.slug, name=m.name) for m in rows}


async def _mentioned(
    db: AsyncSession, world_id: UUID, org_id: UUID, note_rows: Iterable[InternalNote]
) -> dict[str, MemberRef]:
    slugs = {str(slug) for note in note_rows for slug in note.mentions}
    if not slugs:
        return {}
    rows = await db.scalars(
        select(Member).where(
            Member.world_id == world_id, Member.org_id == org_id, Member.slug.in_(slugs)
        )
    )
    return {m.slug: MemberRef(id=m.id, slug=m.slug, name=m.name) for m in rows}


def _ids(values: Iterable[Any]) -> list[UUID]:
    return [UUID(str(value)) for value in values]


def _ref(member_id: UUID | None, members: Mapping[UUID, MemberRef]) -> MemberRef | None:
    return members.get(member_id) if member_id is not None else None


def _refs(values: Iterable[Any], members: Mapping[UUID, MemberRef]) -> list[MemberRef]:
    return [members[member_id] for member_id in _ids(values) if member_id in members]
