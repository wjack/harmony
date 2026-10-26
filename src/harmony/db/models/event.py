"""The event outbox: the SSE feed and replay log, one gap-free sequence per world (§8)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from harmony.db.models.base import Base, CreatedAt, JsonObject, one_of
from harmony.db.models.choices import ScopeKind


class Event(CreatedAt, Base):
    """One durable event. Written only by `UnitOfWork.commit`, never updated.

    `payload` is the event model's JSON (`event.model_dump(mode="json")`, including
    `type`); the scope columns are `EventScope`'s fields, so SSE can filter without
    decoding the payload. For `entity.snapshot` events the entity columns repeat the
    snapshot's type, id and version so a recorded run can be replayed by entity.

    The only foreign key goes to `world_event_seq`, not `worlds`, and is checked
    immediately: it locks only the counter row the committing transaction already
    holds, so the insert takes no new lock after the seq is allocated. For the same
    reason the scope and entity ids carry no foreign keys.
    """

    __tablename__ = "events"
    __table_args__ = (
        one_of("scope_kind", ScopeKind),
        CheckConstraint(
            "num_nulls(entity_type, entity_id, entity_version) IN (0, 3)",
            name="entity_columns",
        ),
    )

    world_id: Mapped[UUID] = mapped_column(
        ForeignKey("world_event_seq.world_id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str]
    scope_kind: Mapped[ScopeKind]
    org_id: Mapped[UUID | None]
    member_id: Mapped[UUID | None]
    thread_id: Mapped[UUID | None]
    session_id: Mapped[UUID | None]
    entity_type: Mapped[str | None]
    entity_id: Mapped[UUID | None]
    entity_version: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[JsonObject]
