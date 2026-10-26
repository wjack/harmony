"""Retiring worlds: the interface the world service calls, and an in-process version.

`fork_and_reset` marks the old world `retiring`, repoints the session, then calls
`Retirer.retire`. Retirement must never block the visitor: the Temporal retirer only
starts `RetireWorldWorkflow` (T7, `worlds/retirer_temporal.py`); `SyncRetirer` is for
tests and Temporal-less runs, where no workflows exist to terminate.

`mark_retired` and `delete_retired_rows` are the database steps both retirers share
(the Temporal one runs them from its activity).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.db import repo
from harmony.db.models import World
from harmony.db.uow import unit_of_work
from harmony.events.schemas import EventScope
from harmony.events.schemas import WorldRetired as WorldRetiredEvent


class Retirer(Protocol):
    async def retire(self, world_id: UUID) -> None:
        """Retire `world_id`, whose status is already `retiring` (or `live`).

        Idempotent: retiring a retired or unknown world is a no-op. On return the
        retirement is durably under way (Temporal) or finished (sync); either way,
        from now on activities and commits for the world raise `WorldRetired`.

        The steps (ARCHITECTURE.md §7): set `worlds.status = 'retired'` and emit
        `world.retired` in one UnitOfWork (`require_live=False`); terminate the world's
        workflows; delete the world's rows after the configured TTL.
        """
        ...


async def mark_retired(sessionmaker: async_sessionmaker[AsyncSession], world_id: UUID) -> bool:
    """Set the world `retired` and emit `world.retired`, in one unit.

    Returns False, changing nothing, if the world is unknown or already retired. The
    row lock makes a concurrent commit in the world either finish first or see the
    new status at its own re-check (and raise `WorldRetired`).
    """
    async with unit_of_work(sessionmaker, world_id, require_live=False) as uow:
        world = await uow.session.get(World, world_id, with_for_update=True)
        if world is None or world.status == "retired":
            return False
        world.status = "retired"
        world.retired_at = datetime.now(UTC)
        uow.add_event(WorldRetiredEvent(), EventScope.world())
        await uow.commit()
        return True


async def delete_retired_rows(
    sessionmaker: async_sessionmaker[AsyncSession], world_id: UUID
) -> None:
    """Delete a retired world's rows, keeping the `worlds` row as a tombstone.

    A no-op unless the world is `retired`, so it never empties a live world.
    """
    async with sessionmaker() as session, session.begin():
        if await repo.world_status(session, world_id) == "retired":
            await repo.delete_world_rows(session, world_id)


class SyncRetirer:
    """Runs the retirement steps inline, minus workflow termination.

    With `row_ttl_s == 0` the rows are deleted before `retire` returns; otherwise they
    are left for the TTL sweep.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        row_ttl_s: float = 0.0,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._row_ttl_s = row_ttl_s

    async def retire(self, world_id: UUID) -> None:
        await mark_retired(self._sessionmaker, world_id)
        if self._row_ttl_s == 0:
            # Also on a repeat call: a retirement interrupted between the two steps
            # finishes here.
            await delete_retired_rows(self._sessionmaker, world_id)
