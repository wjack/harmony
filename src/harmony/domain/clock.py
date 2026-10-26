"""The world clock (§11.1): scenario anchor + (now - world created) + fast-forward.

Seeded times are stored as the wall-clock instant the world clock shows on their date
(`seeds.loader`), so the inverse (`wall_clock`) places a world date back on that axis.
Workflows pass `workflow.now()` as `now`; everything else passes the current time.

Owner: E4.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from harmony.db.models import World
from harmony.seeds.loader import ScenarioNotFound, load_scenario
from harmony.seeds.schema import ScenarioError


def world_now(world: World, anchor: date, now: datetime) -> datetime:
    """The world's current instant."""
    start = datetime.combine(anchor, time(), tzinfo=UTC)
    return start + (now - world.created_at) + timedelta(seconds=world.ff_offset_s)


def world_date(world: World, anchor: date, now: datetime) -> date:
    return world_now(world, anchor, now).date()


def wall_clock(world: World, anchor: date, at: datetime) -> datetime:
    """The real instant at which the world clock shows `at`."""
    start = datetime.combine(anchor, time(), tzinfo=UTC)
    return world.created_at + (at - start) - timedelta(seconds=world.ff_offset_s)


@cache
def scenario_anchor(scenario: str) -> date | None:
    """The scenario's anchor date, or None for an unknown scenario (test worlds)."""
    try:
        return load_scenario(scenario).anchor
    except (ScenarioNotFound, ScenarioError):
        return None


async def today(session: AsyncSession, world_id: UUID, now: datetime | None = None) -> date:
    """The world date now (the real date for a world without a scenario anchor)."""
    at = now or datetime.now(UTC)
    world = await session.get(World, world_id)
    anchor = scenario_anchor(world.scenario) if world is not None else None
    if world is None or anchor is None:
        return at.date()
    return world_date(world, anchor, at)


__all__ = ["scenario_anchor", "today", "wall_clock", "world_date", "world_now"]
