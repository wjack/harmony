"""The API's lifespan: the process's logging, the LISTEN bus, the Temporal retirer with
its inline fallback, and the startup check that retires worlds orphaned by a Temporal
reset (§7, §8, §13)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import httpx
import pytest
import uvicorn
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.testing import WorkflowEnvironment

from harmony.api.app import create_app
from harmony.api.deps import AppServices
from harmony.config import Settings
from harmony.db.models import World, WorldWorkflow
from harmony.events.pg_bus import PgEventBus
from harmony.temporal.start import world_workflow_id
from harmony.worlds.retirer_temporal import FallbackRetirer
from tests.integration._visitors import gated_app
from tests.integration._worlds import create_world

type Sessionmaker = async_sessionmaker[AsyncSession]

# Nothing listens here, so every Temporal call fails to connect.
UNREACHABLE_TEMPORAL = "127.0.0.1:1"


@contextmanager
def _as_uvicorn_starts() -> Iterator[None]:
    """The root logger as a fresh `uvicorn ... --factory` process leaves it: no
    handlers, level WARNING, and only uvicorn's own loggers configured."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    for handler in handlers:
        root.removeHandler(handler)
    root.setLevel(logging.WARNING)
    try:
        uvicorn.Config("harmony.api.app:create_app", factory=True).configure_logging()
        yield
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)


async def _status(sm: Sessionmaker, world_id: UUID) -> str:
    async with sm() as session:
        status = await session.scalar(select(World.status).where(World.id == world_id))
    assert status is not None
    return status


async def _record_gone_workflow(sm: Sessionmaker, world_id: UUID) -> None:
    async with sm() as session, session.begin():
        session.add(
            WorldWorkflow(
                world_id=world_id,
                workflow_id=world_workflow_id(world_id, "conv", "gone"),
                workflow_type="ParkedWorkflow",
            )
        )


async def test_without_temporal_the_api_starts_and_retires_inline(
    app_settings: Settings, db_engine: AsyncEngine, db_sessionmaker: Sessionmaker
) -> None:
    settings = app_settings.model_copy(update={"temporal_address": UNREACHABLE_TEMPORAL})
    world_id = await create_world(db_sessionmaker)
    app = create_app(settings, engine=db_engine)

    async with app.router.lifespan_context(app):
        services: AppServices = app.state.services
        assert isinstance(services.event_bus, PgEventBus)
        assert isinstance(services.retirer, FallbackRetirer)
        async with asyncio.timeout(30):
            await services.retirer.retire(world_id)
        assert await _status(db_sessionmaker, world_id) == "retired"


async def test_startup_retires_worlds_whose_workflows_are_gone(
    app_settings: Settings,
    db_engine: AsyncEngine,
    db_sessionmaker: Sessionmaker,
    temporal_env: WorkflowEnvironment,
) -> None:
    orphaned, unrecorded = await create_world(db_sessionmaker), await create_world(db_sessionmaker)
    # As after a Temporal reset: recorded, but unknown to this server.
    await _record_gone_workflow(db_sessionmaker, orphaned)
    app = create_app(app_settings, engine=db_engine, temporal=temporal_env.client)

    async with app.router.lifespan_context(app):
        check = next(t for t in asyncio.all_tasks() if t.get_name() == "retire-orphaned-worlds")
        async with asyncio.timeout(30):
            assert await check == [orphaned]

    # No worker runs RetireWorldWorkflow here, so the world stays `retiring`.
    assert await _status(db_sessionmaker, orphaned) == "retiring"
    assert await _status(db_sessionmaker, unrecorded) == "live"


async def test_the_api_process_logs_gate_attempts(
    app_settings: Settings, db_engine: AsyncEngine, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = app_settings.model_copy(update={"temporal_address": UNREACHABLE_TEMPORAL})
    app = gated_app(settings, db_engine)

    with _as_uvicorn_starts():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                wrong = await client.post(
                    "/api/gate",
                    json={"passcode": "not the passcode"},
                    headers={"X-Forwarded-For": "198.51.100.7"},
                )
    assert wrong.status_code == 401

    lines = [line for line in capsys.readouterr().err.splitlines() if "gate_attempt" in line]
    assert len(lines) == 1, lines
    assert "INFO harmony.api.gate: gate_attempt outcome=wrong" in lines[0]
    assert "198.51.100.7" not in lines[0]
