"""Internal notes: one org's private side-channel on a thread (§10, UI_REFERENCE §6).

Notes are never visible to the counterparty or in a representative's context.
`@mentions` (member slugs) give the mentioned members `shared` thread access with the
note as the question.

`add` queues an `internal_note` snapshot (append-only, version 1, through
`UnitOfWork.add_event`) to the owning org's thread scope only. `add_unsummarized` is the
one note Claude writes itself: why a side has no closing summary.

Owner: E4.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.db import repo
from harmony.db.models import InternalNote, Member
from harmony.db.models.choices import FailureReason, NoteAuthor
from harmony.db.uow import UnitOfWork
from harmony.domain import threads
from harmony.events.schemas import EntitySnapshot, EventScope

BODY_MAX_CHARS = 4000
INTERNAL_NOTE_ENTITY: Final = "internal_note"
_MENTION = re.compile(r"(?<![\w@])@([a-z0-9]+(?:-[a-z0-9]+)*)")


def parse_mentions(body: str) -> list[str]:
    """The distinct `@slug` mentions in a note body, in order."""
    return list(dict.fromkeys(_MENTION.findall(body)))


async def add(
    uow: UnitOfWork,
    *,
    note_id: UUID,
    thread_id: UUID,
    org_id: UUID,
    author_kind: NoteAuthor,
    body: str,
    author_member_id: UUID | None = None,
    mentions: Sequence[str] = (),
) -> InternalNote:
    """Append a note at the next per-(thread, org) `seq`. Idempotent on `note_id`.

    Mentions that name a member of `org_id` are stored and give that member `shared`
    access with the note as the question; other slugs are dropped."""
    if not body.strip() or len(body) > BODY_MAX_CHARS:
        raise ValueError(f"a note is 1 to {BODY_MAX_CHARS} characters")
    session = uow.session
    existing = await repo.get(session, InternalNote, uow.world_id, note_id)
    if existing is not None:
        return existing
    thread = await threads.locked(uow, thread_id)
    threads.side_of(thread, org_id)
    mentioned = (
        list(
            await session.scalars(
                repo.in_world(Member, uow.world_id).where(
                    Member.org_id == org_id, Member.slug.in_(list(mentions))
                )
            )
        )
        if mentions
        else []
    )
    order = {slug: index for index, slug in enumerate(mentions)}
    mentioned.sort(key=lambda member: order[member.slug])
    last = await session.scalar(
        select(func.max(InternalNote.seq)).where(
            InternalNote.world_id == uow.world_id,
            InternalNote.thread_id == thread_id,
            InternalNote.org_id == org_id,
        )
    )
    note = InternalNote(
        world_id=uow.world_id,
        id=note_id,
        thread_id=thread_id,
        org_id=org_id,
        seq=(last or 0) + 1,
        author_kind=author_kind,
        author_member_id=author_member_id,
        body=body,
        mentions=[member.slug for member in mentioned],
    )
    session.add(note)
    await session.flush()
    await session.refresh(note, attribute_names=["created_at"])
    uow.add_event(
        EntitySnapshot(
            entity_type=INTERNAL_NOTE_ENTITY,
            entity_id=note.id,
            version=1,
            data=threads.row_data(note),
        ),
        EventScope.thread(org_id, thread_id),
    )
    for member in mentioned:
        if member.id != author_member_id:
            await threads.add_access(
                uow,
                thread,
                member_id=member.id,
                org_id=org_id,
                reason="shared",
                shared_by_member_id=author_member_id,
                question=body,
            )
    return note


_UNSUMMARIZED_BECAUSE: Final[Mapping[FailureReason, str]] = {
    "session_budget": "this session's live-model budget had too little left",
    "daily_budget": "the daily live-model budget had too little left",
    "demo_budget": "the demo's live-model budget had too little left",
    "paused": "live models were paused",
    "model_error": "the model service returned an error",
    "refused": "the model declined to go on",
}


async def add_unsummarized(
    uow: UnitOfWork, *, thread_id: UUID, org_id: UUID, turn_id: UUID, reason: FailureReason
) -> InternalNote:
    """Claude's note to `org_id` that its last turn (`turn_id`), after the other side
    closed, failed and wrote no summary. Nothing else says why the summary is missing:
    after a real close the thread's `stop_reason` stays empty, and after the other
    side's refusal (which closes it for that side) the Stopped banner says only why the
    thread ended. Idempotent on the turn."""
    return await add(
        uow,
        note_id=uuid5(turn_id, "unsummarized"),
        thread_id=thread_id,
        org_id=org_id,
        author_kind="claude",
        body=(
            f"Claude couldn't write a closing summary for our side: "
            f"{_UNSUMMARIZED_BECAUSE[reason]} when its last turn ran. The other side "
            "had already closed the conversation."
        ),
    )


async def list_for(
    session: AsyncSession, *, world_id: UUID, thread_id: UUID, org_id: UUID
) -> list[InternalNote]:
    """The org's notes on a thread, oldest first."""
    return list(
        await session.scalars(
            repo.in_world(InternalNote, world_id)
            .where(InternalNote.thread_id == thread_id, InternalNote.org_id == org_id)
            .order_by(InternalNote.seq)
        )
    )


__all__ = [
    "BODY_MAX_CHARS",
    "INTERNAL_NOTE_ENTITY",
    "add",
    "add_unsummarized",
    "list_for",
    "parse_mentions",
]
