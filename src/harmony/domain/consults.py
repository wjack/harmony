"""Consults: a representative's question to its own side (§2.1, §10;
REVISION_UNSTRUCTURED.md §4).

`consult` blocks the representative's turn: the thread goes `awaiting_principal`, and
the answer comes back as the `tool_result` of the `consult` call `tool_use_id`,
whenever it is released (the `ThreadWorkflow` owes it). While a consult is open, the
same question (thread, side, normalized question: `dedupe_key`) reuses it instead of
paging anyone twice. A settled one is never reused: asked again, the question is a new
consult, answered from the records and the policy as they are then (§3, §4.1).

Lifecycle (`ConsultStatus`): `answering` (internal Claude is drafting) → `released`
(by code, `agents.release.decide`, or by a member) | `needs_human` (an escalation is
open) → `released` | `declined`.

The row keeps what the release decision was made on: the answerer's first draft
(`record_draft`, with the turn whose tool calls show what it looked at), the sharing
check's verdict when one ran (`record_sharing_check`), and what reached the
representative and from whom (`release`, or `decline` with what it may say). A redraft
is an escalation draft (`escalations.add_draft`), not a new decision.

Writes queue a `consult` snapshot to the consulting org's thread scope only.

Owner: E2.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import and_, func, or_, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents.models import ActivityLine
from harmony.db import repo
from harmony.db.models import Consult, ModelCall
from harmony.db.models.choices import ConsultStatus, ReleasedBy, Side
from harmony.db.uow import UnitOfWork
from harmony.domain import conversations, records, threads
from harmony.domain.errors import ConsultNotOpen
from harmony.events.schemas import EventScope
from harmony.llm.wire import as_object, as_str, objects

if TYPE_CHECKING:
    from harmony.agents.consult import ConsultDraft, SharingCheck

log = logging.getLogger(__name__)

QUESTION_MAX_CHARS = 2000
OPEN_STATUSES: Final = frozenset[ConsultStatus](("answering", "needs_human"))
_PUNCTUATION: Final = re.compile(r"[^\w\s]")
# Separates a dedupe key from the id that makes a repeat of a settled question unique.
_REPEAT: Final = "#"


def normalize_question(question: str) -> str:
    """Casefolded, punctuation stripped, whitespace collapsed."""
    return " ".join(_PUNCTUATION.sub(" ", question.casefold()).split())


def dedupe_key(thread_id: UUID, side: Side, question: str) -> str:
    """`consult:{thread}:{side}:{sha256 of the casefolded, whitespace-collapsed,
    punctuation-stripped question}`."""
    digest = hashlib.sha256(normalize_question(question).encode()).hexdigest()
    return f"consult:{thread_id}:{side}:{digest}"


def _clip(question: str) -> str:
    question = question.strip()
    if len(question) <= QUESTION_MAX_CHARS:
        return question
    return question[: QUESTION_MAX_CHARS - 1].rstrip() + "…"


def _queue(uow: UnitOfWork, consult: Consult) -> None:
    uow.add_snapshot(consult, EventScope.thread(consult.org_id, consult.thread_id))


async def open(
    uow: UnitOfWork,
    *,
    consult_id: UUID,
    thread_id: UUID,
    side: Side,
    org_id: UUID,
    question: str,
    turn_id: UUID,
    tool_use_id: str,
) -> tuple[Consult, bool]:
    """Open an `answering` consult and move the thread to `awaiting_principal`.

    Returns `(consult, created)`; `created` is False when an open consult with the same
    `dedupe_key` exists (returned unchanged: its answer is owed to the new `tool_use_id`
    too). A settled one with that key doesn't count: the new consult is stored under
    `<dedupe_key>#<consult_id>`, because the key is unique per world. Idempotent on
    `consult_id`. Questions longer than `QUESTION_MAX_CHARS` are clipped.
    """
    session = uow.session
    existing = await repo.get(session, Consult, uow.world_id, consult_id)
    if existing is not None:
        return existing, True
    question = _clip(question)
    key = dedupe_key(thread_id, side, question)
    in_world = repo.in_world(Consult, uow.world_id)
    same = await session.scalar(
        in_world.where(
            Consult.status.in_(OPEN_STATUSES),
            or_(
                Consult.dedupe_key == key,
                Consult.dedupe_key.startswith(f"{key}{_REPEAT}", autoescape=True),
            ),
        ).order_by(Consult.created_at)
    )
    if same is not None:
        return same, False
    taken = await session.scalar(
        select(Consult.id).where(Consult.world_id == uow.world_id, Consult.dedupe_key == key)
    )
    consult = Consult(
        world_id=uow.world_id,
        id=consult_id,
        thread_id=thread_id,
        side=side,
        org_id=org_id,
        question=question,
        status="answering",
        dedupe_key=key if taken is None else f"{key}{_REPEAT}{consult_id}",
        turn_id=turn_id,
        tool_use_id=tool_use_id,
    )
    session.add(consult)
    await session.flush()
    _queue(uow, consult)
    await threads.set_state(uow, thread_id, "awaiting_principal", side_to_move=side)
    return consult, True


async def _locked(uow: UnitOfWork, consult_id: UUID) -> Consult:
    consult = await uow.session.get(
        Consult, (uow.world_id, consult_id), with_for_update=True, populate_existing=True
    )
    if consult is None:
        raise repo.NotFoundInWorld(Consult, uow.world_id, consult_id)
    return consult


async def record_draft(
    uow: UnitOfWork,
    consult_id: UUID,
    draft: ConsultDraft | None,
    *,
    answer_turn_id: UUID,
) -> Consult:
    """Store the draft the release decision is made on, or that the answerer produced
    none (`draft` None), with the answerer's turn. The first write wins, so a retried
    activity or a later redraft leaves it as decided."""
    consult = await _locked(uow, consult_id)
    if consult.answer_turn_id is not None:
        return consult
    consult.answer_turn_id = answer_turn_id
    if draft is not None:
        consult.answer = draft.answer
        consult.needs_human = draft.needs_human
        consult.needs_human_reason = draft.needs_human_reason
        consult.route = draft.route.model_dump(mode="json") if draft.route else None
        consult.rationale = draft.rationale
    await uow.session.flush()
    _queue(uow, consult)
    return consult


async def record_sharing_check(uow: UnitOfWork, consult_id: UUID, check: SharingCheck) -> Consult:
    """Store the sharing check's verdict on the recorded draft. The first write wins."""
    consult = await _locked(uow, consult_id)
    if consult.sharing_check is not None:
        return consult
    consult.sharing_check = check.model_dump(mode="json")
    await uow.session.flush()
    _queue(uow, consult)
    return consult


async def release(
    uow: UnitOfWork,
    consult_id: UUID,
    *,
    text: str,
    released_by: ReleasedBy,
    member_id: UUID | None = None,
) -> Consult:
    """Mark the consult `released` with `text`, the tool result the representative
    receives, released by code (`auto`) or by the member `member_id`. The caller owes
    the tool result. Idempotent once released; raises `ConsultNotOpen` for a declined
    consult."""
    if (released_by == "member") != (member_id is not None):
        raise ValueError("a member release names the member, and only a member release does")
    consult = await _locked(uow, consult_id)
    if consult.status == "released":
        return consult
    if consult.status not in OPEN_STATUSES:
        raise ConsultNotOpen(consult_id, consult.status)
    consult.status = "released"
    consult.released_text = text
    consult.released_by = released_by
    consult.released_by_member_id = member_id
    consult.released_at = datetime.now(UTC)
    await uow.session.flush()
    _queue(uow, consult)
    return consult


async def mark_needs_human(uow: UnitOfWork, consult_id: UUID) -> Consult:
    """`answering` → `needs_human` (an escalation was opened for it). Idempotent."""
    consult = await _locked(uow, consult_id)
    if consult.status == "needs_human":
        return consult
    if consult.status != "answering":
        raise ConsultNotOpen(consult_id, consult.status)
    consult.status = "needs_human"
    await uow.session.flush()
    _queue(uow, consult)
    return consult


async def decline(uow: UnitOfWork, consult_id: UUID, *, text: str, member_id: UUID) -> Consult:
    """`needs_human` (or still `answering`) → `declined` by the member `member_id`.
    `text` is the tool result the representative receives (what it may say, if
    anything): kept as `released_text`, like a release, since it reached the
    representative. The caller owes it. Idempotent once declined."""
    consult = await _locked(uow, consult_id)
    if consult.status == "declined":
        return consult
    if consult.status not in OPEN_STATUSES:
        raise ConsultNotOpen(consult_id, consult.status)
    consult.status = "declined"
    consult.released_text = text
    consult.released_by = "member"
    consult.released_by_member_id = member_id
    consult.released_at = datetime.now(UTC)
    await uow.session.flush()
    _queue(uow, consult)
    return consult


async def get(session: AsyncSession, *, world_id: UUID, consult_id: UUID) -> Consult:
    """The consult, or `repo.NotFoundInWorld`."""
    return await repo.get_required(session, Consult, world_id, consult_id)


async def for_thread(
    session: AsyncSession, *, world_id: UUID, thread_id: UUID, side: Side
) -> list[Consult]:
    """One side's consults on a thread, oldest first (the workspace panel)."""
    query = (
        repo.in_world(Consult, world_id)
        .where(Consult.thread_id == thread_id, Consult.side == side)
        .order_by(Consult.created_at, Consult.id)
    )
    return list(await session.scalars(query))


async def ordinal(session: AsyncSession, consult: Consult) -> int:
    """The consult's 1-based position among (thread, side)'s consults by `created_at`
    (ties by id): the answerer's cassette `turn` (`agents.keys`)."""
    if "created_at" in sa_inspect(consult).unloaded:
        await session.refresh(consult, attribute_names=["created_at"])
    created_at = consult.created_at
    count = await session.scalar(
        select(func.count())
        .select_from(Consult)
        .where(
            Consult.world_id == consult.world_id,
            Consult.thread_id == consult.thread_id,
            Consult.side == consult.side,
            or_(
                Consult.created_at < created_at,
                and_(Consult.created_at == created_at, Consult.id <= consult.id),
            ),
        )
    )
    return int(count or 1)


async def activity(
    session: AsyncSession,
    *,
    world_id: UUID,
    org_id: UUID,
    turn_id: UUID,
    steps: range | None = None,
) -> list[ActivityLine]:
    """What `org_id`'s answerer looked at in its turn `turn_id` (`consults.answer_turn_id`,
    or a draft's `turn_id`): one line per tool call, in order, read from the turn's
    checkpointed model calls. Every rev of a consult shares the turn, so `steps`
    (`keys.answerer_steps(rev)`) keeps one rev's calls."""
    statement = select(ModelCall.response_text).where(ModelCall.turn_id == turn_id)
    if steps is not None:
        statement = statement.where(ModelCall.step_idx.between(steps.start, steps.stop - 1))
    responses = await session.scalars(statement.order_by(ModelCall.step_idx))
    calls: list[tuple[str, JsonValue | None]] = []
    for text in responses:
        message = as_object(json.loads(text))
        for block in objects(message.get("content") if message is not None else None):
            name = as_str(block.get("name"))
            if block.get("type") == "tool_use" and name:
                calls.append((name, block.get("input")))
    titles = await records.titles(
        session, world_id=world_id, org_id=org_id, ids=records.read_ids(calls)
    )
    lines: list[ActivityLine] = []
    for name, tool_input in calls:
        label, detail = conversations.tool_label(name, tool_input, titles)
        lines.append(ActivityLine(tool=name, label=label, detail=detail))
    return lines


__all__ = [
    "OPEN_STATUSES",
    "QUESTION_MAX_CHARS",
    "activity",
    "decline",
    "dedupe_key",
    "for_thread",
    "get",
    "mark_needs_human",
    "normalize_question",
    "open",
    "ordinal",
    "record_draft",
    "record_sharing_check",
    "release",
]
