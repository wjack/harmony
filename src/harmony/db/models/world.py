"""Worlds: one sandboxed copy of the network, its event counter and its workflows."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Integer, text
from sqlalchemy.orm import Mapped, mapped_column

from harmony.db.models.base import Base, CreatedAt, WorldScoped, deferred_fk, one_of
from harmony.db.models.choices import WorldMode, WorldStatus


class World(CreatedAt, Base):
    """A world. Not itself world-scoped: every world-scoped row references it.

    Retirement (§7) moves `status` from `live` to `retiring` to `retired`; commits re-check
    it (`UnitOfWork`). The row is kept as a tombstone after the contents are deleted.
    """

    __tablename__ = "worlds"
    __table_args__ = (one_of("mode", WorldMode), one_of("status", WorldStatus))

    id: Mapped[UUID] = mapped_column(primary_key=True)
    scenario: Mapped[str]
    mode: Mapped[WorldMode]
    status: Mapped[WorldStatus] = mapped_column(server_default=text("'live'"))
    # The build that created the world; a visit under another build forks (§11.1).
    build_sha: Mapped[str]
    # Shared by every world forked from the same session, so Reset never refills a
    # budget (`budgets.key = 'lineage:<id>'`).
    budget_lineage: Mapped[UUID]
    # World clock: anchor + (now - created_at) + ff_offset_s (§11.1).
    ff_offset_s: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    retired_at: Mapped[datetime | None]


class WorldEventSeq(Base):
    """The per-world event counter, kept apart from `worlds` so allocating a seq never
    contends with status reads or retirement.

    Created with its world. `UnitOfWork.commit` row-locks it as the transaction's
    last write (`UPDATE … RETURNING seq`), which serializes event writers per world.
    """

    __tablename__ = "world_event_seq"

    world_id: Mapped[UUID] = mapped_column(
        deferred_fk("worlds.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))


class WorldWorkflow(CreatedAt, WorldScoped, Base):
    """A Temporal workflow started for a world, recorded by `start_world_workflow`.

    Retirement terminates these by ID before sweeping the `WorldId` list query,
    and API startup uses them to find `live` worlds whose workflows are gone (§7, §13).
    """

    __tablename__ = "world_workflows"

    workflow_id: Mapped[str] = mapped_column(primary_key=True)
    workflow_type: Mapped[str]
