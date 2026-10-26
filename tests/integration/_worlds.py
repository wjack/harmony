"""Test helpers: create a world the way the seed loader and world service do."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.db import repo
from harmony.db.uow import unit_of_work
from harmony.events.schemas import EventScope, WorldCreated


async def create_world(
    sessionmaker: async_sessionmaker[AsyncSession], *, scenario: str = "test"
) -> UUID:
    """A new live world with its event counter, emitting `world.created` as seq 1."""
    world_id = uuid4()
    async with unit_of_work(sessionmaker, world_id) as uow:
        repo.add_world(
            uow.session,
            world_id=world_id,
            scenario=scenario,
            mode="live",
            build_sha="test",
            budget_lineage=uuid4(),
        )
        uow.add_event(
            WorldCreated(scenario=scenario, mode="live", build_sha="test"), EventScope.world()
        )
        await uow.commit()
    return world_id
