"""World-scoped repository helpers. Every lookup takes the `world_id` (§4).

There is deliberately no get-by-bare-id helper: seed IDs repeat in every world, so an
id alone does not name a row. Build other queries from `in_world(...)`, which starts
with the world filter.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import ColumnElement, Select, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.db.models import Base, Entity, World, WorldEventSeq, WorldScoped
from harmony.db.models.choices import WorldMode, WorldStatus


class NotFoundInWorld(LookupError):
    """No row with this id in this world (it may exist in another)."""

    def __init__(self, model: type[Entity], world_id: UUID, entity_id: UUID) -> None:
        super().__init__(f"{model.__name__} {entity_id} not found in world {world_id}")
        self.world_id = world_id
        self.entity_id = entity_id


def in_world[M: WorldScoped](model: type[M], world_id: UUID) -> Select[M]:
    """`SELECT model WHERE world_id = :world_id`; add filters and ordering to it."""
    return select(model).where(model.world_id == world_id)


async def get[E: Entity](
    session: AsyncSession, model: type[E], world_id: UUID, entity_id: UUID
) -> E | None:
    """The `(world_id, id)` row, from the identity map when already loaded."""
    return await session.get(model, (world_id, entity_id))


async def get_required[E: Entity](
    session: AsyncSession, model: type[E], world_id: UUID, entity_id: UUID
) -> E:
    """Like `get`, but raises `NotFoundInWorld` (API routes turn it into a 404)."""
    row = await get(session, model, world_id, entity_id)
    if row is None:
        raise NotFoundInWorld(model, world_id, entity_id)
    return row


async def list_in_world[M: WorldScoped](
    session: AsyncSession,
    model: type[M],
    world_id: UUID,
    *where: ColumnElement[bool],
) -> Sequence[M]:
    """Every row of `model` in the world matching `where`, unordered."""
    return (await session.scalars(in_world(model, world_id).where(*where))).all()


def add_world(
    session: AsyncSession,
    *,
    world_id: UUID,
    scenario: str,
    mode: WorldMode,
    build_sha: str,
    budget_lineage: UUID,
    ff_offset_s: int = 0,
) -> World:
    """Add a `live` world and its event counter to the session (not yet flushed).

    Call it inside the `unit_of_work(sm, world_id)` that seeds the world and emits
    `world.created`: the unit flushes before its liveness re-check, so the re-check
    sees the new row.
    """
    world = World(
        id=world_id,
        scenario=scenario,
        mode=mode,
        status="live",
        build_sha=build_sha,
        budget_lineage=budget_lineage,
        ff_offset_s=ff_offset_s,
    )
    session.add_all([world, WorldEventSeq(world_id=world_id, seq=0)])
    return world


async def world_status(session: AsyncSession, world_id: UUID) -> WorldStatus | None:
    """The world's status, or None if there is no such world."""
    return (
        await session.execute(select(World.status).where(World.id == world_id))
    ).scalar_one_or_none()


async def delete_world_rows(session: AsyncSession, world_id: UUID) -> None:
    """Delete every row a retired world owns, keeping its `worlds` row as a tombstone.

    Children go first (reverse dependency order); foreign keys are checked at commit.
    Sessions that still point at the world's members lose those pointers (SET NULL).
    Raises `ValueError` unless the world is `retired`, so a live world is never emptied.
    """
    status = await world_status(session, world_id)
    if status != "retired":
        raise ValueError(f"world {world_id} is {status or 'missing'}, not retired")
    for table in reversed(Base.metadata.sorted_tables):
        if table.name != World.__tablename__ and "world_id" in table.c:
            await session.execute(delete(table).where(table.c.world_id == world_id))
