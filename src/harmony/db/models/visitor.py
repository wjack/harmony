"""Visitor sessions and spend budgets: the tables that are not world-scoped."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from harmony.db.models.base import DEFERRED, Base, CreatedAt, deferred_fk, one_of
from harmony.db.models.choices import ReservationStatus


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
    budget_lineage: Mapped[UUID]
    access_code_hash: Mapped[str | None]
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Budget(Base):
    """A hosted-demo spend cap (§14), reserved atomically:
    `UPDATE budgets … WHERE spent_usd + reserved_usd + :est <= cap_usd`.

    Keys: `dev`, `lineage:<id>`, `access:<hash>`, `daily:<date>`, `live-network`.
    """

    __tablename__ = "budgets"
    __table_args__ = (CheckConstraint("spent_usd >= 0 AND reserved_usd >= 0", name="non_negative"),)

    key: Mapped[str] = mapped_column(primary_key=True)
    cap_usd: Mapped[Decimal]
    spent_usd: Mapped[Decimal] = mapped_column(server_default=text("0"))
    reserved_usd: Mapped[Decimal] = mapped_column(server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BudgetReservation(CreatedAt, Base):
    """One in-flight reservation against several budget keys, settled after the call."""

    __tablename__ = "budget_reservations"
    __table_args__ = (one_of("status", ReservationStatus),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    keys: Mapped[list[str]] = mapped_column(postgresql.ARRAY(Text()))
    est_usd: Mapped[Decimal]
    status: Mapped[ReservationStatus] = mapped_column(server_default=text("'reserved'"))
    settled_usd: Mapped[Decimal | None]
    settled_at: Mapped[datetime | None]
