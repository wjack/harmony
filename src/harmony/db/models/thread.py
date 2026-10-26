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
    VersionedEntity,
    WorldScoped,
    one_of,
    world_fk,
)
from harmony.db.models.choices import (
    AccessReason,
    NoteAuthor,
    Provenance,
    Side,
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
