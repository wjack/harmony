"""Inbox route: what's waiting on the acting member (§10, UI_REFERENCE §6).

- GET /api/inbox: the five sections

Sections:
- `needs_you`: open escalations assigned to the acting member;
- `with_your_team`: live threads with an escalation open for someone else on the team
  (one routed or reassigned to a teammate): a person, not Claude, holds them up;
- `claude_handling`: the member's visible threads that are live with no escalation open,
  including ones whose escalation they resolved (the conversation goes on);
- `shared_with_you`: threads a teammate shared (`thread_access.reason = shared`) that
  aren't already in `needs_you`;
- `done`: threads that ended (`closed`, `expired`), most recent first, last 50.

Carries `X-Harmony-Cursor`; `escalation` and `thread` snapshots keep it live.

Owner: E4.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Final, NamedTuple
from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents import release
from harmony.agents.models import EscalationStatus, MemberRef
from harmony.api.deps import SessionmakerDep, read_cursor, set_cursor, snapshot_session
from harmony.api.routes.problems import problems
from harmony.auth import access
from harmony.auth.session import Actor, ActorDep
from harmony.auth.views import OrgRef
from harmony.db.models import (
    Consult,
    Conversation,
    Escalation,
    EscalationDraft,
    Member,
    NetworkRequest,
    Org,
    Thread,
    ThreadAccess,
)
from harmony.db.models.choices import (
    AccessReason,
    EscalationKind,
    FailureReason,
    Side,
    ThreadState,
)
from harmony.domain import escalations, threads

router = APIRouter(tags=["inbox"])

DONE_LIMIT: Final = 50


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class InboxEscalation(_View):
    id: UUID
    kind: EscalationKind
    status: EscalationStatus
    # The consult question, or a one-line summary of a held message.
    question: str
    # The answerer's one-line decision for the reviewer; None without a draft that
    # has one.
    short_question: str | None = None
    # "Why this needs you": the answerer's or the sharing check's reason, or the mode.
    reason: str
    assignees: list[MemberRef]
    created_at: datetime
    version: int


class InboxItem(_View):
    thread_id: UUID
    # As the member's side names the thread (`threads.thread_title`).
    title: str
    # The request's chat when the acting member owns it: a question their side's
    # representative asks them shows there, inline, and is answered there (§7).
    conversation_id: UUID | None
    side: Side
    counterparty: OrgRef
    thread_state: ThreadState
    # Why the thread stopped early (a cap, the pause, a model error); None otherwise.
    stop_reason: FailureReason | None = None
    # Whose turn it is; while `awaiting_principal`, the side checking with its team.
    side_to_move: Side | None
    # The open escalation the row is about, else the latest one the member resolved.
    escalation: InboxEscalation | None
    # `shared_with_you`: who shared it and what they asked.
    shared_by: MemberRef | None
    shared_question: str | None
    access_reasons: list[AccessReason]
    last_activity_at: datetime


class Inbox(_View):
    needs_you: list[InboxItem]
    with_your_team: list[InboxItem]
    claude_handling: list[InboxItem]
    shared_with_you: list[InboxItem]
    done: list[InboxItem]


@router.get("/inbox", response_model=Inbox, responses=problems(cursor=True))
async def get_inbox(actor: ActorDep, sessionmaker: SessionmakerDep, response: Response) -> Inbox:
    async with snapshot_session(sessionmaker) as db:
        inbox = await _inbox(db, actor)
        cursor = await read_cursor(db, actor.world_id)
    set_cursor(response, cursor)
    return inbox


async def _inbox(db: AsyncSession, actor: Actor) -> Inbox:
    visible = list(await db.scalars(access.visible_threads(actor)))
    reasons = await access.access_reasons(db, actor, visible)
    thread_ids = [thread.id for thread in visible]
    rows = list(
        await db.scalars(
            select(Escalation)
            .where(
                Escalation.world_id == actor.world_id,
                Escalation.org_id == actor.org_id,
                Escalation.thread_id.in_(thread_ids),
                (Escalation.status == "open")
                | (Escalation.resolved_by_member_id == actor.member_id),
            )
            .order_by(Escalation.created_at, Escalation.id)
        )
    )
    shares = {
        share.thread_id: share
        for share in await db.scalars(
            select(ThreadAccess)
            .where(
                ThreadAccess.world_id == actor.world_id,
                ThreadAccess.member_id == actor.member_id,
                ThreadAccess.reason == "shared",
            )
            .order_by(ThreadAccess.created_at)
        )
    }
    views = _Views(
        requests=await _requests(db, actor, [thread.request_id for thread in visible]),
        openings=await threads.opening_lines(
            db,
            actor.world_id,
            [thread.id for thread in visible if thread.requester_org_id != actor.org_id],
        ),
        orgs=await _orgs(db, actor.world_id, visible),
        members=await _members(db, actor.world_id, rows, shares.values()),
        escalations=await escalation_texts(db, actor.world_id, rows),
    )
    needs_you: list[tuple[Escalation, InboxItem]] = []
    with_your_team: list[InboxItem] = []
    claude_handling: list[InboxItem] = []
    shared_with_you: list[InboxItem] = []
    done: list[InboxItem] = []
    for thread in visible:
        on_thread = [row for row in rows if row.thread_id == thread.id]
        open_rows = [row for row in on_thread if row.status == "open"]
        mine = [row for row in open_rows if actor.member_id in escalations.assignee_ids(row)]
        resolved = [row for row in on_thread if row.resolved_by_member_id == actor.member_id]
        latest = max(resolved, key=_resolved_at, default=None)
        share = shares.get(thread.id)
        reasons_here = reasons.get(thread.id, [])
        if mine:
            item = views.item(thread, actor.org_id, mine[0], share, reasons_here)
            needs_you.append((mine[0], item))
        elif thread.state in threads.TERMINAL_STATES:
            done.append(views.item(thread, actor.org_id, latest, share, reasons_here))
        elif share is not None:
            shown = open_rows[0] if open_rows else latest
            shared_with_you.append(views.item(thread, actor.org_id, shown, share, reasons_here))
        elif open_rows:
            with_your_team.append(
                views.item(thread, actor.org_id, open_rows[0], None, reasons_here)
            )
        else:
            claude_handling.append(views.item(thread, actor.org_id, latest, None, reasons_here))
    needs_you.sort(key=lambda pair: pair[0].created_at)
    return Inbox(
        needs_you=[item for _, item in needs_you],
        with_your_team=_recent_first(with_your_team),
        claude_handling=_recent_first(claude_handling),
        shared_with_you=_recent_first(shared_with_you),
        done=_recent_first(done)[:DONE_LIMIT],
    )


class _Views:
    """The lookups an inbox row is built from."""

    def __init__(
        self,
        *,
        requests: Mapping[UUID, _Request],
        openings: Mapping[UUID, str],
        orgs: Mapping[UUID, OrgRef],
        members: Mapping[UUID, MemberRef],
        escalations: Mapping[UUID, EscalationText],
    ) -> None:
        self._requests = requests
        self._openings = openings
        self._orgs = orgs
        self._members = members
        self._escalations = escalations

    def item(
        self,
        thread: Thread,
        org_id: UUID,
        escalation: Escalation | None,
        share: ThreadAccess | None,
        reasons: list[AccessReason],
    ) -> InboxItem:
        side = threads.side_of(thread, org_id)
        counterparty = threads.org_of(thread, threads.other(side))
        request = self._requests.get(thread.request_id)
        return InboxItem(
            thread_id=thread.id,
            title=threads.thread_title(
                thread,
                request.row,
                org_id,
                requester=self._orgs[thread.requester_org_id].name,
                opening=self._openings.get(thread.id),
            )
            if request is not None
            else "",
            conversation_id=request.chat_id if request is not None else None,
            side=side,
            counterparty=self._orgs[counterparty],
            thread_state=thread.state,
            stop_reason=thread.stop_reason,
            side_to_move=thread.side_to_move,
            escalation=self._escalation(escalation) if escalation is not None else None,
            shared_by=(
                self._members.get(share.shared_by_member_id)
                if share is not None and share.shared_by_member_id is not None
                else None
            ),
            shared_question=share.question if share is not None else None,
            access_reasons=reasons,
            last_activity_at=thread.last_activity_at,
        )

    def _escalation(self, row: Escalation) -> InboxEscalation:
        text = self._escalations[row.id]
        return InboxEscalation(
            id=row.id,
            kind=row.kind,
            status=escalations.escalation_status(row.status),
            question=text.question,
            short_question=text.short_question,
            reason=text.reason,
            assignees=[
                self._members[member_id]
                for member_id in escalations.assignee_ids(row)
                if member_id in self._members
            ],
            created_at=row.created_at,
            version=row.version,
        )


class EscalationText(_View):
    """How an inbox item reads: its question, the answerer's one-line version of it,
    and why it needs a person."""

    question: str
    short_question: str | None = None
    reason: str


HELD_MESSAGE_QUESTION: Final = "Review the outgoing message"
# Why a held message (`outbound_review`) waits, for one whose rev 1 doesn't say.
HELD_MESSAGE_REASON: Final = "A teammate approves every message in Ask mode."
# A representative's turn Claude declined (`refused_turn`).
REFUSED_TURN_QUESTION: Final = "Claude declined to continue the conversation"
REFUSED_TURN_REASON: Final = "Retry Claude's turn, or end the conversation."


def held_reason(rationale: str | None) -> str:
    """Why a held message waits: the reason its hold gave (rev 1's `rationale`)."""
    return rationale or HELD_MESSAGE_REASON


async def escalation_texts(
    db: AsyncSession, world_id: UUID, rows: Sequence[Escalation]
) -> dict[UUID, EscalationText]:
    """Each escalation's question, its current draft's short question, and "why this
    needs you" (`release.why_needed`, the current draft's own reason first)."""
    consult_ids = [row.consult_id for row in rows if row.consult_id is not None]
    asked = {
        consult.id: consult
        for consult in await db.scalars(
            select(Consult).where(Consult.world_id == world_id, Consult.id.in_(consult_ids))
        )
    }
    current = [(row.id, row.current_draft_rev) for row in rows if row.current_draft_rev is not None]
    drafts = (
        await db.execute(
            select(
                EscalationDraft.escalation_id,
                EscalationDraft.short_question,
                EscalationDraft.held_reason,
            ).where(
                EscalationDraft.world_id == world_id,
                tuple_(EscalationDraft.escalation_id, EscalationDraft.draft_rev).in_(current),
            )
        )
        if current
        else ()
    )
    short_questions: dict[UUID, str | None] = {}
    held_reasons: dict[UUID, str | None] = {}
    for draft in drafts:
        short_questions[draft.escalation_id] = draft.short_question
        held_reasons[draft.escalation_id] = draft.held_reason
    held = [row.id for row in rows if row.kind == "outbound_review"]
    reasons: dict[UUID, str] = (
        dict(
            (
                await db.execute(
                    select(EscalationDraft.escalation_id, EscalationDraft.rationale).where(
                        EscalationDraft.world_id == world_id,
                        EscalationDraft.escalation_id.in_(held),
                        EscalationDraft.draft_rev == 1,
                    )
                )
            ).tuples()
        )
        if held
        else {}
    )
    texts: dict[UUID, EscalationText] = {}
    for row in rows:
        if row.kind == "refused_turn":
            texts[row.id] = EscalationText(
                question=REFUSED_TURN_QUESTION, reason=REFUSED_TURN_REASON
            )
            continue
        consult = asked.get(row.consult_id) if row.consult_id is not None else None
        texts[row.id] = (
            EscalationText(
                question=consult.question,
                short_question=short_questions.get(row.id),
                reason=release.why_needed(consult, held_reasons.get(row.id)),
            )
            if consult is not None
            else EscalationText(
                question=HELD_MESSAGE_QUESTION, reason=held_reason(reasons.get(row.id))
            )
        )
    return texts


class _Request(NamedTuple):
    row: NetworkRequest
    # Its chat, if the acting member owns it.
    chat_id: UUID | None


async def _requests(
    db: AsyncSession, actor: Actor, request_ids: list[UUID]
) -> dict[UUID, _Request]:
    rows = await db.execute(
        select(NetworkRequest, Conversation.id)
        .outerjoin(
            Conversation,
            (Conversation.world_id == NetworkRequest.world_id)
            & (Conversation.id == NetworkRequest.conversation_id)
            & (Conversation.owner_member_id == actor.member_id),
        )
        .where(NetworkRequest.world_id == actor.world_id, NetworkRequest.id.in_(request_ids))
    )
    return {request.id: _Request(request, chat_id) for request, chat_id in rows.all()}


async def _orgs(db: AsyncSession, world_id: UUID, visible: Sequence[Thread]) -> dict[UUID, OrgRef]:
    org_ids = {org_id for thread in visible for org_id in threads.parties(thread)}
    rows = await db.scalars(select(Org).where(Org.world_id == world_id, Org.id.in_(org_ids)))
    return {org.id: OrgRef(id=org.id, slug=org.slug, name=org.name) for org in rows}


async def _members(
    db: AsyncSession,
    world_id: UUID,
    rows: Iterable[Escalation],
    shares: Iterable[ThreadAccess],
) -> dict[UUID, MemberRef]:
    member_ids = {member_id for row in rows for member_id in escalations.assignee_ids(row)}
    member_ids |= {s.shared_by_member_id for s in shares if s.shared_by_member_id is not None}
    found = await db.scalars(
        select(Member).where(Member.world_id == world_id, Member.id.in_(member_ids))
    )
    return {m.id: MemberRef(id=m.id, slug=m.slug, name=m.name) for m in found}


def _resolved_at(row: Escalation) -> datetime:
    return row.resolved_at or row.created_at


def _recent_first(items: list[InboxItem]) -> list[InboxItem]:
    return sorted(items, key=lambda item: item.last_activity_at, reverse=True)
