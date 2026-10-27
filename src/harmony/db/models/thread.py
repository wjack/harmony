"""Threads between two orgs' representatives, who can see them, and each side's frozen
representative session (§2.2, §7, §10)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Index, Integer, UniqueConstraint, func
from sqlalchemy import text as sql_text  # `RepNotice.text` would shadow `text`
from sqlalchemy.orm import Mapped, mapped_column

from harmony.db.models.base import (
    Base,
    CreatedAt,
    Entity,
    JsonArray,
    JsonObject,
    VersionedEntity,
    WorldScoped,
    one_of,
    world_fk,
)
from harmony.db.models.choices import (
    AccessReason,
    EndReason,
    FailureReason,
    NoteAuthor,
    Provenance,
    Side,
    SteerDraftOutcome,
    SteerFailure,
    SteerGuidanceOutcome,
    SteerKind,
    SteerStatus,
    ThreadMessageKind,
    ThreadState,
    TranscriptRole,
)


class Thread(CreatedAt, VersionedEntity, Base):
    """One bilateral thread per (request, responder). Its workflow is
    `w:{world}:thread:{id}`, and `state` mirrors the workflow's state machine (§7).

    Each side's prompt-facing settings version is pinned at that side's first turn.
    """

    __tablename__ = "threads"
    __table_args__ = (
        world_fk("request_id", "network_requests"),
        world_fk("requester_org_id", "orgs"),
        world_fk("responder_org_id", "orgs"),
        world_fk(
            ("requester_org_id:org_id", "requester_settings_version:version"),
            "rep_settings",
            name="fk_threads_requester_settings",
        ),
        world_fk(
            ("responder_org_id:org_id", "responder_settings_version:version"),
            "rep_settings",
            name="fk_threads_responder_settings",
        ),
        Index(None, "world_id", "request_id"),
        Index(None, "world_id", "requester_org_id"),
        Index(None, "world_id", "responder_org_id"),
        one_of("state", ThreadState),
        one_of("side_to_move", Side),
        one_of("stop_reason", FailureReason),
        one_of("end_reason", EndReason),
        one_of("closed_by", Side),
        CheckConstraint("requester_org_id <> responder_org_id", name="bilateral"),
    )
    entity_type = "thread"

    request_id: Mapped[UUID]
    requester_org_id: Mapped[UUID]
    responder_org_id: Mapped[UUID]
    state: Mapped[ThreadState] = mapped_column(server_default=sql_text("'active'"))
    side_to_move: Mapped[Side | None]
    requester_settings_version: Mapped[int | None] = mapped_column(Integer)
    responder_settings_version: Mapped[int | None] = mapped_column(Integer)
    # Capped at 16 (§5).
    turn_count: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    deadline_at: Mapped[datetime | None]
    # The network view's edge recency.
    last_activity_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # This conversation's subject: the recipient's title from the card, which both
    # sides see (`threads.thread_title`). NULL on threads opened without one.
    title: Mapped[str | None]
    # Why the thread stopped early (a cap, the pause, a model error); NULL otherwise.
    stop_reason: Mapped[FailureReason | None]
    # The current run: each reopen starts the next one (`thread_runs`). No CHECK ties
    # `end_reason` to `state`: rows that ended before it existed have it NULL.
    run: Mapped[int] = mapped_column(Integer, server_default=sql_text("1"))
    # How and when the current run ended, and which side's representative closed it (or
    # refused); NULL while it is open.
    end_reason: Mapped[EndReason | None]
    ended_at: Mapped[datetime | None]
    closed_by: Mapped[Side | None]
    # When the current run's workflow finished its tail; Reopen waits for it.
    run_finished_at: Mapped[datetime | None]


class ThreadMessage(CreatedAt, Entity, Base):
    """What crossed the network, in thread order. Never updated once delivered (§5).

    IDs minted in an activity are `uuid5(turn_id, step_idx, tool_use_id)`, so a retried
    turn collides instead of duplicating.
    """

    __tablename__ = "thread_messages"
    __table_args__ = (
        world_fk("thread_id", "threads"),
        world_fk("from_org_id", "orgs"),
        world_fk("author_member_id", "members"),
        world_fk("approved_by_member_id", "members"),
        UniqueConstraint("world_id", "thread_id", "seq"),
        one_of("kind", ThreadMessageKind),
        one_of("provenance", Provenance),
    )

    thread_id: Mapped[UUID]
    seq: Mapped[int] = mapped_column(Integer)
    from_org_id: Mapped[UUID]
    # `close` marks where a side closed the thread; its summary never crosses (it is the
    # closer's `rep_sessions.close_summary`).
    kind: Mapped[ThreadMessageKind]
    body: Mapped[str]
    provenance: Mapped[Provenance]
    # A human who took over (`provenance = 'human'`).
    author_member_id: Mapped[UUID | None]
    # The member who approved an agent's message (`provenance = 'agent_approved'`).
    approved_by_member_id: Mapped[UUID | None]
    turn_id: Mapped[UUID | None]


class ThreadAccess(CreatedAt, WorldScoped, Base):
    """Why a member can see a thread: routed to them, shared with them, or admin (§10).

    One row per reason, so a reassign that removes `routed` keeps a `shared` row.
    """

    __tablename__ = "thread_access"
    __table_args__ = (
        world_fk("thread_id", "threads"),
        world_fk("member_id", "members"),
        world_fk("shared_by_member_id", "members"),
        Index(None, "world_id", "member_id"),
        one_of("reason", AccessReason),
    )

    thread_id: Mapped[UUID] = mapped_column(primary_key=True)
    member_id: Mapped[UUID] = mapped_column(primary_key=True)
    reason: Mapped[AccessReason] = mapped_column(primary_key=True)
    shared_by_member_id: Mapped[UUID | None]
    # The @mention question that came with a share.
    question: Mapped[str | None]


class InternalNote(CreatedAt, Entity, Base):
    """Org-only discussion on a thread. Never visible to a representative or the
    counterparty unless someone releases it (§10)."""

    __tablename__ = "internal_notes"
    __table_args__ = (
        world_fk("thread_id", "threads"),
        world_fk("org_id", "orgs"),
        world_fk("author_member_id", "members"),
        UniqueConstraint("world_id", "thread_id", "org_id", "seq"),
        one_of("author_kind", NoteAuthor),
        CheckConstraint(
            "(author_kind = 'member') = (author_member_id IS NOT NULL)",
            name="author_member",
        ),
    )

    thread_id: Mapped[UUID]
    org_id: Mapped[UUID]
    seq: Mapped[int] = mapped_column(Integer)
    author_kind: Mapped[NoteAuthor]
    author_member_id: Mapped[UUID | None]
    body: Mapped[str]
    # Member ids @mentioned in the body.
    mentions: Mapped[JsonArray] = mapped_column(server_default=sql_text("'[]'::jsonb"))


class Steer(CreatedAt, VersionedEntity, Base):
    """A member's instruction to its side's Claude on a thread: an Edit, or the text a
    Reopen starts with (EDIT.md §2.1). Never a comment, and never shown to the
    counterparty: its snapshot goes to the org's thread scope only, without `draft_text`.

    The id is minted by the server from (world, org, member, the client's key)
    (`steers.steer_id_for`).
    """

    __tablename__ = "steers"
    __table_args__ = (
        world_fk("thread_id", "threads"),
        world_fk("org_id", "orgs"),
        world_fk("author_member_id", "members"),
        world_fk("escalation_id", "escalations"),
        UniqueConstraint("world_id", "thread_id", "side", "seq"),
        one_of("side", Side),
        one_of("kind", SteerKind),
        one_of("status", SteerStatus),
        one_of("guidance_outcome", SteerGuidanceOutcome),
        one_of("draft_outcome", SteerDraftOutcome),
        one_of("failure_reason", SteerFailure),
        CheckConstraint("char_length(body) BETWEEN 1 AND 4000", name="body_length"),
        CheckConstraint("char_length(draft_text) <= 20000", name="draft_text_length"),
    )
    entity_type = "steer"
    snapshot_omits = frozenset({"draft_text"})

    thread_id: Mapped[UUID]
    org_id: Mapped[UUID]
    # Which representative the instruction is for.
    side: Mapped[Side]
    # Per (thread, side), from 1: the cassette turn, and the guard's watermark.
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[SteerKind]
    author_member_id: Mapped[UUID]
    body: Mapped[str]
    # The waiting item the member was looking at, which this steer rewrites.
    escalation_id: Mapped[UUID | None]
    # The member's unsaved wording of that item's draft, which the rewrite starts from.
    draft_text: Mapped[str | None]
    # The rev reserved for the rewrite.
    draft_rev: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[SteerStatus] = mapped_column(server_default=sql_text("'working'"))
    # NULL while running.
    guidance_outcome: Mapped[SteerGuidanceOutcome | None]
    # The `rep_guidance.version` this steer wrote.
    guidance_version: Mapped[int | None] = mapped_column(Integer)
    # NULL when there was no item to rewrite.
    draft_outcome: Mapped[SteerDraftOutcome | None]
    failure_reason: Mapped[SteerFailure | None]


class RepGuidance(CreatedAt, Entity, Base):
    """What one side's representative follows on a thread, written by its internal
    Claude from members' instructions (EDIT.md §2.2). Append-only: each steer that
    changes it adds the next version, and the empty string means withdrawn.

    The id is `uuid5(steer_id, "rep_guidance")`, so a retried write finds its row.
    """

    __tablename__ = "rep_guidance"
    __table_args__ = (
        world_fk("thread_id", "threads"),
        world_fk("org_id", "orgs"),
        world_fk("steer_id", "steers"),
        world_fk("created_by_member_id", "members"),
        UniqueConstraint("world_id", "thread_id", "side", "version"),
        UniqueConstraint("world_id", "steer_id"),
        one_of("side", Side),
        CheckConstraint("char_length(text) <= 2000", name="text_length"),
    )

    thread_id: Mapped[UUID]
    org_id: Mapped[UUID]
    side: Mapped[Side]
    # Per (thread, side), from 1.
    version: Mapped[int] = mapped_column(Integer)
    text: Mapped[str]
    # The instruction that prompted it, and who typed that instruction.
    steer_id: Mapped[UUID]
    created_by_member_id: Mapped[UUID]


class RepSession(CreatedAt, WorldScoped, Base):
    """One representative session per (thread, side): the system prompt and tool list,
    rendered at that side's first turn and stored as exact bytes, never changed after
    (§2). Org-private, so it also holds the side's close summary.
    """

    __tablename__ = "rep_sessions"
    __table_args__ = (world_fk("thread_id", "threads"), one_of("side", Side))

    thread_id: Mapped[UUID] = mapped_column(primary_key=True)
    side: Mapped[Side] = mapped_column(primary_key=True)
    system_text: Mapped[str]
    tools_text: Mapped[str]
    model: Mapped[str]
    # Written by this side's `close` (or its last turn after the other side closed);
    # for its own org only, never sent to the counterparty (§5).
    close_summary: Mapped[str | None]


_REOPENED = "reopened_by_member_id, reopened_by_org_id, reopened_side, steer_id"


class ThreadRun(WorldScoped, Base):
    """One run of a thread: the first, and one per reopen (REOPEN.md §7.1). Private
    engine state, never snapshotted. A thread with no row here is seeded history.
    """

    __tablename__ = "thread_runs"
    __table_args__ = (
        world_fk("thread_id", "threads"),
        world_fk("reopened_by_member_id", "members"),
        world_fk("reopened_by_org_id", "orgs"),
        world_fk("steer_id", "steers"),
        one_of("reopened_side", Side),
        one_of("state", ThreadState),
        one_of("end_reason", EndReason),
        one_of("closed_by", Side),
        one_of("stop_reason", FailureReason),
        # Run 1 opened with the thread; every later run is a member's reopen.
        CheckConstraint(
            f"(run = 1 AND num_nonnulls({_REOPENED}) = 0)"
            f" OR (run > 1 AND num_nulls({_REOPENED}) = 0)",
            name="reopened",
        ),
    )

    thread_id: Mapped[UUID] = mapped_column(primary_key=True)
    run: Mapped[int] = mapped_column(Integer, primary_key=True)
    # What the run started with.
    started_at: Mapped[datetime]
    deadline_at: Mapped[datetime | None]
    max_turns: Mapped[int] = mapped_column(Integer)
    # Who reopened it, from which side, with which steer; NULL for run 1.
    reopened_by_member_id: Mapped[UUID | None]
    reopened_by_org_id: Mapped[UUID | None]
    reopened_side: Mapped[Side | None]
    steer_id: Mapped[UUID | None]
    # The highest message seq when the run began (the close guard, the timeline).
    start_seq: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    # How the run ended; NULL until it does.
    state: Mapped[ThreadState | None]
    end_reason: Mapped[EndReason | None]
    closed_by: Mapped[Side | None]
    stop_reason: Mapped[FailureReason | None]
    ended_at: Mapped[datetime | None]
    # Each side's close summary, archived here at the next reopen; each is shown only to
    # its own org.
    requester_summary: Mapped[str | None]
    responder_summary: Mapped[str | None]
    # The `ThreadCarry` the next run resumes from, written by `finish_run`.
    carry: Mapped[JsonObject | None]
    finished_at: Mapped[datetime | None]


class RepNotice(CreatedAt, WorldScoped, Base):
    """A pending notice to a representative from its side or from Harmony (settings
    delta, mode change, world date, the counterparty closed). Flushed as one merged
    system message at the side's next model call, then stamped with that turn (§2)."""

    __tablename__ = "rep_notices"
    __table_args__ = (world_fk("thread_id", "threads"), one_of("side", Side))

    thread_id: Mapped[UUID] = mapped_column(primary_key=True)
    side: Mapped[Side] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str]
    text: Mapped[str]
    flushed_turn_id: Mapped[UUID | None]


class RepTranscript(CreatedAt, WorldScoped, Base):
    """A representative side's transcript, append-only: each message's raw wire JSON,
    exactly as sent or received, ordered by `seq` (§2.2)."""

    __tablename__ = "rep_transcript"
    __table_args__ = (
        world_fk("thread_id", "threads"),
        one_of("side", Side),
        one_of("role", TranscriptRole),
    )

    thread_id: Mapped[UUID] = mapped_column(primary_key=True)
    side: Mapped[Side] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[TranscriptRole]
    content_text: Mapped[str]
    turn_id: Mapped[UUID | None]
