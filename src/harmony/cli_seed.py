"""`harmony seed --scenario <name>`: load a scenario into a new world.

Prints the world ID and the world's row count per table. The world belongs to no visitor
session; the API creates its own worlds lazily from `POST /api/session/start`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.config import get_settings
from harmony.db.engine import create_sessionmaker, engine_from_settings
from harmony.db.models import Base, World
from harmony.seeds.loader import (
    DEFAULT_SCENARIO,
    SCENARIOS_DIR,
    ScenarioNotFound,
    available_scenarios,
    load_scenario,
    load_world,
)
from harmony.seeds.schema import ScenarioError

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def seed(
    scenario: Annotated[
        str, typer.Option(help="Scenario directory name under --scenarios-dir.")
    ] = DEFAULT_SCENARIO,
    scenarios_dir: Annotated[
        Path, typer.Option(help="Where scenarios live.", file_okay=False)
    ] = SCENARIOS_DIR,
) -> None:
    """Load a scenario into a new world and print its ID and row counts."""
    try:
        parsed = load_scenario(scenario, scenarios_dir)
    except ScenarioNotFound as exc:
        known = ", ".join(available_scenarios(scenarios_dir)) or "none"
        typer.echo(f"{exc} (available: {known})", err=True)
        raise typer.Exit(code=1) from exc
    except ScenarioError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    world_id, counts = asyncio.run(_seed(parsed.name, scenarios_dir))
    typer.echo(f"world {world_id} (scenario {parsed.name})")
    width = max(len(table) for table in counts)
    for table, count in counts.items():
        typer.echo(f"  {table:<{width}}  {count:>5}")


async def _seed(scenario: str, root: Path) -> tuple[UUID, dict[str, int]]:
    settings = get_settings()
    engine = engine_from_settings(settings, application_name="harmony-seed")
    try:
        sessionmaker = create_sessionmaker(engine)
        world_id = await load_world(sessionmaker, scenario, build_sha=settings.build_sha, root=root)
        return world_id, await _row_counts(sessionmaker, world_id)
    finally:
        await engine.dispose()


async def _row_counts(
    sessionmaker: async_sessionmaker[AsyncSession], world_id: UUID
) -> dict[str, int]:
    """Non-zero row counts of the world's tables, in dependency order."""
    counts: dict[str, int] = {}
    async with sessionmaker() as session:
        for table in Base.metadata.sorted_tables:
            if table.name == World.__tablename__:
                column = table.c.id
            elif "world_id" in table.c:
                column = table.c.world_id
            else:
                continue
            count = await session.scalar(
                select(func.count()).select_from(table).where(column == world_id)
            )
            if count:
                counts[table.name] = count
    return counts
