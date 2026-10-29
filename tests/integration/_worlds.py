"""Test helpers: create a world the way the seed loader and world service do."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.db import repo
from harmony.db.models import Member
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
        )
        uow.add_event(
            WorldCreated(scenario=scenario, mode="live", build_sha="test"), EventScope.world()
        )
        await uow.commit()
    return world_id


async def demote_to_member(
    sessionmaker: async_sessionmaker[AsyncSession], world_id: UUID, member_slug: str
) -> None:
    """Make a seeded member a non-admin: every seeded member is an admin, so tests of
    the admin check need one who isn't. Takes effect on the member's next request."""
    async with sessionmaker() as session:
        demoted = await session.scalar(
            update(Member)
            .where(Member.world_id == world_id, Member.slug == member_slug)
            .values(role="member", version=Member.version + 1)
            .returning(Member.id)
        )
        assert demoted is not None, f"no member {member_slug}"
        await session.commit()
