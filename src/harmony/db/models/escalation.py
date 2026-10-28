"""Consults, the inbox items that wait on a human, and the answerer's drafts (§4, §6)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Index, Integer, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from harmony.db.models.base import (
    Base,
    CreatedAt,
    JsonArray,
    JsonObject,
    VersionedEntity,
    WorldScoped,
    one_of,
    world_fk,
)
from harmony.db.models.choices import ConsultStatus, EscalationKind, ReleasedBy, Side


class Consult(CreatedAt, VersionedEntity, Base):
    """A representative's question to its own side. While one is open the thread is
    `awaiting_principal` and no representative turn runs (§4).

    `dedupe_key` is derived from (thread, side, normalized question). The answer returns
    as the result of the `consult` tool call `tool_use_id`.
    """

    __tablename__ = "consults"
    __table_args__ = (
        world_fk("thread_id", "threads"),
        world_fk("org_id", "orgs"),
        world_fk("released_by_member_id", "members"),
        world_fk("same_as_id", "consults"),
        world_fk("drafted_from_id", "escalations", cycle=True),
        UniqueConstraint("world_id", "dedupe_key"),
        Index(None, "world_id", "thread_id"),
        one_of("side", Side),
        one_of("status", ConsultStatus),
        one_of("released_by", ReleasedBy),
        CheckConstraint(
            "coalesce(released_by = 'member', false) = (released_by_member_id IS NOT NULL)",
            name="released_by_member",
        ),
    )
    entity_type = "consult"

    thread_id: Mapped[UUID]
    side: Mapped[Side]
    # The consulting side's org.
    org_id: Mapped[UUID]
    question: Mapped[str]
    status: Mapped[ConsultStatus] = mapped_column(server_default=text("'answering'"))
    dedupe_key: Mapped[str]
    turn_id: Mapped[UUID | None]
    tool_use_id: Mapped[str | None]
    # The answerer's draft the release decision was made on (`ConsultDraft`); NULL until
    # one is written, and still NULL when the answerer produced none.
    answer: Mapped[str | None]
    needs_human: Mapped[bool | None]
    needs_human_reason: Mapped[str | None]
    # Assignee slugs plus a one-line reason (`agents.models.Route`).
    route: Mapped[JsonObject | None]
    rationale: Mapped[str | None]
    # The answerer's turn: its tool calls (`model_calls`) are what it looked at.
    answer_turn_id: Mapped[UUID | None]
    # `{within_sharing, reason}` (`agents.consult.SharingCheck`); NULL when none ran.
    sharing_check: Mapped[JsonObject | None]
    # The `consult` tool result the representative received, and who released it.
    released_text: Mapped[str | None]
    released_by: Mapped[ReleasedBy | None]
    released_by_member_id: Mapped[UUID | None]
    released_at: Mapped[datetime | None]
    # The earlier consult on the request that Claude judged asks the same question:
    # always the group's first, the card the chat shows. NULL when none did.
    same_as_id: Mapped[UUID | None]
    # On a card: the organizations of the askers its answer went to, as org id strings,
    # added when the chat's owner answers (the shown asker and the ones kept ticked).
    goes_to_org_ids: Mapped[JsonArray] = mapped_column(server_default=text("'[]'::jsonb"))
    # The escalation whose member answer this consult's draft started from (a reused
    # answer); NULL for a draft of its own.
    drafted_from_id: Mapped[UUID | None]


class Escalation(CreatedAt, VersionedEntity, Base):
    """An inbox item: a consult answer, an outgoing message, or a representative's turn
    Claude declined, waiting on a person (§4, §6).

    Resolved through a Temporal Update that writes `WHERE status = 'open'`, so a second
    resolver loses cleanly. `status` is free text (`agents.models.EscalationStatus`).
    """

    __tablename__ = "escalations"
    __table_args__ = (
        world_fk("org_id", "orgs"),
        world_fk("thread_id", "threads"),
        world_fk("consult_id", "consults"),
        world_fk("resolved_by_member_id", "members"),
        UniqueConstraint("world_id", "dedupe_key"),
        Index(None, "world_id", "org_id", "status"),
        one_of("side", Side),
        one_of("kind", EscalationKind),
        CheckConstraint("kind <> 'consult' OR consult_id IS NOT NULL", name="subject_present"),
    )
    entity_type = "escalation"

    org_id: Mapped[UUID]
    thread_id: Mapped[UUID | None]
    side: Mapped[Side | None]
    kind: Mapped[EscalationKind]
    consult_id: Mapped[UUID | None]
    # The representative's turn Claude declined (`refused_turn`): the item's subject.
    turn_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(server_default=text("'open'"))
    dedupe_key: Mapped[str]
    # Member ids the item is routed to.
    assignees: Mapped[JsonArray] = mapped_column(server_default=text("'[]'::jsonb"))
    # The draft the inbox shows; drafts carry their own rev so a redraft never
    # conflicts with a resolution.
    current_draft_rev: Mapped[int | None] = mapped_column(Integer)
    resolution: Mapped[JsonObject | None]
    resolved_by_member_id: Mapped[UUID | None]
    resolved_at: Mapped[datetime | None]


class EscalationDraft(CreatedAt, WorldScoped, Base):
    """One draft for an escalation: an answerer draft of a consult answer, or the held
    message of an `outbound_review` (rev 1, the representative's). Each rewrite a
    member's Edit asks for is a new rev (`turn_id` is then the steer's)."""

    __tablename__ = "escalation_drafts"
    __table_args__ = (world_fk("escalation_id", "escalations"),)

    escalation_id: Mapped[UUID] = mapped_column(primary_key=True)
    draft_rev: Mapped[int] = mapped_column(Integer, primary_key=True)
    answer: Mapped[str]
    # Assignees, each with why it is theirs (`agents.models.Route`).
    route: Mapped[JsonObject | None]
    rationale: Mapped[str] = mapped_column(server_default=text("''"))
    # The decision in one line, for the reviewer's card; NULL on older drafts.
    short_question: Mapped[str | None]
    # The turn that wrote it: for a consult, the answerer's (what it looked at).
    turn_id: Mapped[UUID | None]
    # Why a rev drafted from a member's earlier answer waits for a person: the sharing
    # check's reason, or the answerer's own. The consult's `needs_human_reason` and
    # `sharing_check` are rev 1's only.
    held_reason: Mapped[str | None]
