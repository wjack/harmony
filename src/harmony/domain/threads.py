"""Threads: one per (request, responder), and what crosses the network (§4, §7).

A thread's `state` mirrors its `ThreadWorkflow`. Messages are append-only, ordered by
a per-thread `seq` allocated under the thread's row lock, and never updated once
delivered. Message ids are `side_effect_id(turn_id, step_idx, tool_use_id)` for agent
messages, so a retried turn collides instead of duplicating.

Writes queue, for each party org with `EventScope.thread(org, thread)`, a
`thread_message` snapshot (append-only, version 1, through `UnitOfWork.add_event` as
P0's turn does) and the `thread` snapshot.

A thread's own `title` is the recipient's title from the card, written to be seen by
both sides, so both name the thread by it (`thread_title`). Without one (threads from
before titles), each side falls back to what it may see: the requester to the
request's title (`requester_title`: the stored one, else the brief's first line), and
a responder to what crossed, the requester's first message on the thread, else
"Request from X". A responder never sees the request's own title, which is written
for the requester's member.

`cassette_key` names a thread for ScriptedModel cassettes (`agents.keys.thread_key`):
the seed slug for a seeded thread, else `<requester>.<responder>[.<k>]`, where k counts
only the non-seeded threads between the same pair. Everything that keys a model call on
a thread (turns, the consult answerer) uses it, so the keys agree.

Owner: E4.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from functools import cache
from typing import Final
from uuid import UUID

from pydantic import JsonValue, TypeAdapter
from pydantic_core import to_jsonable_python
from sqlalchemy import func, select, tuple_
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents.keys import thread_key
from harmony.agents.models import BRIEF_TITLE_CHARS, OutreachRecipient, brief_title, first_line
from harmony.db import repo
from harmony.db.models import (
    Base,
    NetworkRequest,
    Org,
    RepSession,
    Thread,
    ThreadAccess,
    ThreadMessage,
    ThreadRun,
    World,
)
from harmony.db.models.choices import (
    AccessReason,
    EndReason,
    FailureReason,
    Provenance,
    Side,
    ThreadMessageKind,
    ThreadState,
)
from harmony.db.uow import UnitOfWork
from harmony.events.schemas import EntitySnapshot, EventScope, ThreadAccessChanged
from harmony.seeds.ids import seed_id
from harmony.seeds.loader import ScenarioNotFound, load_scenario
from harmony.seeds.schema import ScenarioError

MAX_TURNS = 16
TERMINAL_STATES: frozenset[ThreadState] = frozenset(("closed", "expired"))
THREAD_MESSAGE_ENTITY: Final = "thread_message"
# What either party's stream carries about a message, as `GET /threads/{id}` shows it:
# its provenance (§5), never which teammate approved or wrote it, nor the turn.
MESSAGE_SNAPSHOT_COLUMNS: Final = (
    "id",
    "thread_id",
    "seq",
    "from_org_id",
    "kind",
    "body",
    "provenance",
    "created_at",
)
_RECIPIENTS: TypeAdapter[list[OutreachRecipient]] = TypeAdapter(list[OutreachRecipient])


async def open(
    uow: UnitOfWork,
    *,
    thread_id: UUID,
    request: NetworkRequest,
    responder_org_id: UUID,
    deadline_at: datetime | None,
) -> Thread:
    """Open the thread `active` with the requester to move, titled with the request's
    title for this recipient, record its first run, and give the request's creator
    `routed` access on the requester side. Idempotent on `thread_id`. The responder
    side's access is routed when its first escalation is (§10); its admins see it
    regardless."""
    session = uow.session
    existing = await repo.get(session, Thread, uow.world_id, thread_id)
    if existing is not None:
        return existing
    thread = Thread(
        world_id=uow.world_id,
        id=thread_id,
        request_id=request.id,
        requester_org_id=request.requester_org_id,
        responder_org_id=responder_org_id,
        state="active",
        side_to_move="requester",
        turn_count=0,
        deadline_at=deadline_at,
        title=_recipient_title(request, responder_org_id),
    )
    session.add(thread)
    await session.flush()
    session.add(
        ThreadRun(
            world_id=uow.world_id,
            thread_id=thread_id,
            run=1,
            started_at=datetime.now(UTC),
            deadline_at=deadline_at,
            max_turns=MAX_TURNS,
            start_seq=0,
        )
    )
    await session.flush()
    if request.created_by_member_id is not None:
        await add_access(
            uow, thread, member_id=request.created_by_member_id, org_id=request.requester_org_id
        )
    snapshot(uow, thread)
    return thread


async def add_access(
    uow: UnitOfWork,
    thread: Thread,
    *,
    member_id: UUID,
    org_id: UUID,
    reason: AccessReason = "routed",
    shared_by_member_id: UUID | None = None,
    question: str | None = None,
) -> bool:
    """Give a member access to the thread for `reason`; False when they already had it
    for that reason. Tells the member (`thread.access_changed`, member scope)."""
    inserted = await uow.session.scalar(
        insert(ThreadAccess)
        .values(
            world_id=uow.world_id,
            thread_id=thread.id,
            member_id=member_id,
            reason=reason,
            shared_by_member_id=shared_by_member_id,
            question=question,
        )
        .on_conflict_do_nothing()
        .returning(ThreadAccess.member_id)
    )
    if inserted is None:
        return False
    uow.add_event(
        ThreadAccessChanged(
            thread_id=thread.id,
            member_id=member_id,
            reason=reason,
            added=True,
            shared_by=shared_by_member_id,
            question=question,
        ),
        EventScope.member(member_id, org_id),
    )
    return True


async def append_message(
    uow: UnitOfWork,
    thread_id: UUID,
    *,
    message_id: UUID,
    from_org_id: UUID,
    kind: ThreadMessageKind,
    body: str,
    provenance: Provenance,
    turn_id: UUID | None = None,
    author_member_id: UUID | None = None,
    approved_by_member_id: UUID | None = None,
) -> ThreadMessage:
    """Append one message and update the thread: `side_to_move` (the other side
    answers next), `last_activity_at`, and `turn_count`, which counts turns, not
    messages: a representative may send several messages in one turn (§5), so only a
    message that starts a new turn counts (see `_starts_turn`). Returns the existing row
    when `message_id` was already written."""
    session = uow.session
    thread = await locked(uow, thread_id)
    existing = await repo.get(session, ThreadMessage, uow.world_id, message_id)
    if existing is not None:
        return existing
    if from_org_id not in (thread.requester_org_id, thread.responder_org_id):
        raise ValueError(f"org {from_org_id} is not a party to thread {thread_id}")
    last = await session.scalar(
        repo.in_world(ThreadMessage, uow.world_id)
        .where(ThreadMessage.thread_id == thread_id)
        .order_by(ThreadMessage.seq.desc())
        .limit(1)
    )
    message = ThreadMessage(
        world_id=uow.world_id,
        id=message_id,
        thread_id=thread_id,
        seq=(last.seq if last is not None else 0) + 1,
        from_org_id=from_org_id,
        kind=kind,
        body=body,
        provenance=provenance,
        author_member_id=author_member_id,
        approved_by_member_id=approved_by_member_id,
        turn_id=turn_id,
    )
    session.add(message)
    if _starts_turn(last, from_org_id=from_org_id, turn_id=turn_id):
        thread.turn_count += 1
    thread.side_to_move = "requester" if from_org_id == thread.responder_org_id else "responder"
    thread.last_activity_at = datetime.now(UTC)
    await session.flush()
    await session.refresh(message, attribute_names=["created_at"])
    delivered = EntitySnapshot(
        entity_type=THREAD_MESSAGE_ENTITY,
        entity_id=message.id,
        # Delivered messages are never updated (§5), so there is one version.
        version=1,
        data={key: to_jsonable_python(getattr(message, key)) for key in MESSAGE_SNAPSHOT_COLUMNS},
    )
    for org_id in parties(thread):
        uow.add_event(delivered, EventScope.thread(org_id, thread.id))
    snapshot(uow, thread)
    return message


def _starts_turn(last: ThreadMessage | None, *, from_org_id: UUID, turn_id: UUID | None) -> bool:
    """Whether a message opens a new turn: the first message, a side change, or a new
    agent turn. A person's message (no `turn_id`) is a turn of its own."""
    return (
        last is None
        or last.from_org_id != from_org_id
        or turn_id is None
        or last.turn_id != turn_id
    )


async def set_state(
    uow: UnitOfWork,
    thread_id: UUID,
    state: ThreadState,
    *,
    side_to_move: Side | None = None,
    end_reason: EndReason | None = None,
    closed_by: Side | None = None,
    stop_reason: FailureReason | None = None,
) -> Thread:
    """Move the thread to `state`. A terminal thread leaves its state only through
    `reopen`, which starts its next run.

    The move into a terminal state stores how the run ended: `end_reason` (required
    then), `closed_by` (the side whose representative closed it, or refused),
    `stop_reason` (why the system ended it: a refused reservation, a model error, a
    refusal) and `ended_at`."""
    if state in TERMINAL_STATES and end_reason is None:
        raise ValueError(f"thread {thread_id} can't end without an end_reason")
    thread = await locked(uow, thread_id)
    if thread.state in TERMINAL_STATES or (
        thread.state == state and thread.side_to_move == side_to_move
    ):
        return thread
    now = datetime.now(UTC)
    thread.state = state
    thread.side_to_move = None if state in TERMINAL_STATES else side_to_move
    if state in TERMINAL_STATES:
        thread.end_reason = end_reason
        thread.closed_by = closed_by
        thread.stop_reason = stop_reason
        thread.ended_at = now
    thread.last_activity_at = now
    snapshot(uow, thread)
    return thread


async def reopen(
    uow: UnitOfWork,
    thread_id: UUID,
    *,
    run: int,
    side_to_move: Side,
    deadline_at: datetime | None,
) -> Thread:
    """Start the ended thread's run `run` (REOPEN.md §4.4): live again with the
    reopening side to move, a new deadline, and nothing left of how the last run
    ended. The caller holds the thread lock and has checked it may reopen."""
    thread = await locked(uow, thread_id)
    if thread.state not in TERMINAL_STATES:
        raise ValueError(f"thread {thread_id} is {thread.state}, not ended")
    thread.state = "active"
    thread.side_to_move = side_to_move
    thread.run = run
    thread.end_reason = None
    thread.ended_at = None
    thread.closed_by = None
    thread.stop_reason = None
    thread.run_finished_at = None
    thread.deadline_at = deadline_at
    thread.last_activity_at = datetime.now(UTC)
    snapshot(uow, thread)
    return thread


async def runs(session: AsyncSession, world_id: UUID, thread_id: UUID) -> list[ThreadRun]:
    """The thread's runs, first to latest; none for seeded history."""
    rows = await session.scalars(
        repo.in_world(ThreadRun, world_id)
        .where(ThreadRun.thread_id == thread_id)
        .order_by(ThreadRun.run)
    )
    return list(rows)


async def last_seq(session: AsyncSession, world_id: UUID, thread_id: UUID) -> int:
    """The thread's highest message seq (0 before any message)."""
    seq = await session.scalar(
        select(func.max(ThreadMessage.seq)).where(
            ThreadMessage.world_id == world_id, ThreadMessage.thread_id == thread_id
        )
    )
    return seq or 0


async def store_close_summary(uow: UnitOfWork, thread_id: UUID, side: Side, summary: str) -> None:
    """The side's own close summary (`rep_sessions.close_summary`): for its org only,
    never sent to the counterparty (§5). The first one written stands, so a retried
    `close` or a late approval of a held one never replaces it.

    The side's org gets a newer `thread` snapshot, which is what refreshes its views of
    the thread: a summary written in the last turn, after the thread closed, would
    otherwise reach them only on a reload. The counterparty gets none."""
    row = await uow.session.get(
        RepSession, (uow.world_id, thread_id, side), with_for_update=True, populate_existing=True
    )
    if row is None:
        # Only a side whose representative took a turn can close; no retry fixes this.
        raise ValueError(f"thread {thread_id} has no {side} representative session")
    if row.close_summary is not None:
        return
    row.close_summary = summary.strip()
    thread = await locked(uow, thread_id)
    thread.last_activity_at = datetime.now(UTC)
    await uow.session.flush()
    uow.add_snapshot(thread, EventScope.thread(org_of(thread, side), thread.id))


async def opening_lines(
    session: AsyncSession, world_id: UUID, thread_ids: Collection[UUID]
) -> dict[UUID, str]:
    """Each thread's first message from the requester, as a title (`opening_title`;
    threads with none are left out)."""
    rows = await session.execute(
        select(ThreadMessage.thread_id, ThreadMessage.body)
        .join(
            Thread,
            (Thread.world_id == ThreadMessage.world_id) & (Thread.id == ThreadMessage.thread_id),
        )
        .where(
            ThreadMessage.world_id == world_id,
            ThreadMessage.thread_id.in_(thread_ids),
            ThreadMessage.from_org_id == Thread.requester_org_id,
            ThreadMessage.kind == "message",
        )
        .order_by(ThreadMessage.thread_id, ThreadMessage.seq)
        .ext(distinct_on(ThreadMessage.thread_id))
    )
    return {thread_id: opening_title(body) for thread_id, body in rows.all()}


# A leading sentence: up to its end mark, or the end of its line.
_LEADING_SENTENCE: Final = re.compile(r"\s*([^\n.!?]*[.!?]*)")
_NAMES_CLAUDE: Final = re.compile(r"\bClaude\b")


def opening_title(body: str) -> str:
    """A requester's opening message as a title: its first line once the leading
    salutation and self-introduction ("Hello, this is Claude for …") are set aside.
    Representatives introduce themselves, and the introduction names the sender, not
    the request: left in, every request from an org reads the same. The message itself
    is unchanged; a message that is all greeting keeps its first line."""
    rest = body
    while (sentence := _LEADING_SENTENCE.match(rest)) is not None and _is_greeting(
        sentence.group(1)
    ):
        rest = rest[sentence.end() :]
    return first_line(rest, BRIEF_TITLE_CHARS) or first_line(body, BRIEF_TITLE_CHARS)


def _is_greeting(sentence: str) -> bool:
    """A bare salutation ("Hello!", "Hi there.") or a sentence naming the sender."""
    words = len(sentence.split())
    return 0 < words <= 2 or (words > 0 and _NAMES_CLAUDE.search(sentence) is not None)


def thread_title(
    thread: Thread, request: NetworkRequest, org_id: UUID, *, requester: str, opening: str | None
) -> str:
    """How `org_id`'s members name `thread` (module docstring): its own title; else the
    requester's side by `requester_title`, and a responder by the requester's first
    message on it (`opening`), else as a request from `requester` (the org's name)."""
    if thread.title:
        return thread.title
    if org_id == request.requester_org_id:
        return requester_title(request)
    return responder_title(requester=requester, opening=opening)


def requester_title(request: NetworkRequest) -> str:
    """The request's name on the requester's side and the network lens: its title, else
    its brief's first line (`brief_title`)."""
    return request.title or brief_title(request.brief)


def responder_title(*, requester: str, opening: str | None) -> str:
    """A responder's name for an untitled thread: the requester's first message on it,
    else a request from `requester` (the org's name)."""
    return opening or f"Request from {requester}"


def _recipient_title(request: NetworkRequest, org_id: UUID) -> str | None:
    recipients = _RECIPIENTS.validate_python(request.recipients)
    return next((row.title for row in recipients if row.org_id == org_id), None)


def parties(thread: Thread) -> tuple[UUID, UUID]:
    return (thread.requester_org_id, thread.responder_org_id)


def side_of(thread: Thread, org_id: UUID) -> Side:
    """The side `org_id` is on; raises `ValueError` for an org that isn't a party."""
    if org_id == thread.requester_org_id:
        return "requester"
    if org_id == thread.responder_org_id:
        return "responder"
    raise ValueError(f"org {org_id} is not a party to thread {thread.id}")


def state_for(thread: Thread, org_id: UUID | None) -> ThreadState:
    """The thread's state as `org_id` may know it (None: the network lens, which sees
    every side). A side checking with its own team is that side's business: to anyone
    else its `awaiting_principal` reads `active`."""
    if org_id is None or thread.state != "awaiting_principal":
        return thread.state
    if org_id in parties(thread) and side_of(thread, org_id) == thread.side_to_move:
        return thread.state
    return "active"


def org_of(thread: Thread, side: Side) -> UUID:
    return thread.requester_org_id if side == "requester" else thread.responder_org_id


def other(side: Side) -> Side:
    return "responder" if side == "requester" else "requester"


async def cassette_key(session: AsyncSession, thread: Thread) -> str:
    """The thread's ScriptedModel key (module docstring)."""
    scenario = await session.scalar(select(World.scenario).where(World.id == thread.world_id))
    seeded = seeded_threads(scenario or "")
    if thread.id in seeded:
        return thread_key("", "", seeded_slug=seeded[thread.id])
    slugs = dict(
        (
            await session.execute(
                select(Org.id, Org.slug).where(
                    Org.world_id == thread.world_id,
                    Org.id.in_(parties(thread)),
                )
            )
        ).all()
    )
    earlier = await session.scalars(
        select(Thread.id).where(
            Thread.world_id == thread.world_id,
            Thread.requester_org_id == thread.requester_org_id,
            Thread.responder_org_id == thread.responder_org_id,
            tuple_(Thread.created_at, Thread.id) <= tuple_(thread.created_at, thread.id),
        )
    )
    k = sum(1 for thread_id in earlier if thread_id not in seeded)
    return thread_key(slugs[thread.requester_org_id], slugs[thread.responder_org_id], max(k, 1))


@cache
def seeded_threads(scenario: str) -> Mapping[UUID, str]:
    """Seeded thread ids of a scenario, with their slugs (none for an unknown one)."""
    try:
        loaded = load_scenario(scenario)
    except (ScenarioNotFound, ScenarioError):
        return {}
    return {seed_id(scenario, "thread", spec.slug): spec.slug for spec in loaded.threads}


def row_data(row: Base) -> dict[str, JsonValue]:
    """Every column but `world_id`, as JSON (the shape `entity.snapshot` carries)."""
    columns = [attr.key for attr in sa_inspect(row).mapper.column_attrs if attr.key != "world_id"]
    return {key: to_jsonable_python(getattr(row, key)) for key in columns}


def snapshot(uow: UnitOfWork, thread: Thread) -> None:
    """Queue the thread's snapshot to both parties' thread scope."""
    for org_id in parties(thread):
        uow.add_snapshot(thread, EventScope.thread(org_id, thread.id))


async def locked(uow: UnitOfWork, thread_id: UUID) -> Thread:
    """The thread, row-locked: serializes writers of one thread (seq, state)."""
    thread = await uow.session.get(
        Thread, (uow.world_id, thread_id), with_for_update=True, populate_existing=True
    )
    if thread is None:
        raise repo.NotFoundInWorld(Thread, uow.world_id, thread_id)
    return thread


__all__ = [
    "MAX_TURNS",
    "MESSAGE_SNAPSHOT_COLUMNS",
    "TERMINAL_STATES",
    "THREAD_MESSAGE_ENTITY",
    "add_access",
    "append_message",
    "cassette_key",
    "last_seq",
    "locked",
    "open",
    "opening_lines",
    "opening_title",
    "org_of",
    "other",
    "parties",
    "reopen",
    "requester_title",
    "responder_title",
    "row_data",
    "runs",
    "seeded_threads",
    "set_state",
    "side_of",
    "snapshot",
    "state_for",
    "store_close_summary",
    "thread_title",
]
