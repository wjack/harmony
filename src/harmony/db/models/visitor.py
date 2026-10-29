"""Visitor sessions and the live switch: the tables that are not world-scoped."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKeyConstraint, Index, Integer, func, text
from sqlalchemy.orm import Mapped, mapped_column

from harmony.db.models.base import DEFERRED, Base, CreatedAt, deferred_fk


class VisitorSession(CreatedAt, Base):
    """A visitor (§11.1). The signed cookie holds only `id`; everything else is here.

    A session holds two worlds, each with its acting member. The composite foreign keys
    make the database refuse an acting member from another world (act-as returns 403
    before it gets that far). If a world's rows are deleted, its pointers go NULL and
    the session gets a fresh world on its next start.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["desk_world_id", "desk_member_id"],
            ["members.world_id", "members.id"],
            ondelete="SET NULL",
            **DEFERRED,
        ),
        ForeignKeyConstraint(
            ["network_world_id", "network_member_id"],
            ["members.world_id", "members.id"],
            ondelete="SET NULL",
            **DEFERRED,
        ),
        Index(None, "desk_world_id"),
        Index(None, "network_world_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    desk_world_id: Mapped[UUID | None] = mapped_column(
        deferred_fk("worlds.id", ondelete="SET NULL")
    )
    desk_member_id: Mapped[UUID | None]
    network_world_id: Mapped[UUID | None] = mapped_column(
        deferred_fk("worlds.id", ondelete="SET NULL")
    )
    network_member_id: Mapped[UUID | None]
    # Bumped on act-as; SSE streams opened under an older epoch close (§8).
    epoch: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LiveControls(Base):
    """The live switch (3.4): one row, key `live`, inserted by the migration.

    Every live call from a guard with a database reads it first (`llm.live_switch`),
    and a missing row refuses (fail closed). It is a row rather than an env var so
    pausing is instant.
    """

    __tablename__ = "live_controls"

    key: Mapped[str] = mapped_column(primary_key=True)
    paused: Mapped[bool] = mapped_column(server_default=text("false"))
    provider_blocked_until: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
