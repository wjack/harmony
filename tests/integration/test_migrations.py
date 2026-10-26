"""The migrations round-trip and match the models (`alembic check`)."""

from __future__ import annotations

import asyncio
import os
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from harmony.config import REPO_ROOT
from tests.conftest import ClonedDatabase


def _config(database: ClonedDatabase) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database.url
    config.attributes["configure_logging"] = False
    return config


async def _tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        return {row.tablename for row in rows}


async def test_downgrade_to_base_then_upgrade_to_head_round_trips_with_no_drift(
    database: ClonedDatabase, db_engine: AsyncEngine
) -> None:
    config = _config(database)
    # Alembic's env.py runs its own event loop, so drive it from a worker thread.
    await asyncio.to_thread(command.check, config)

    await asyncio.to_thread(command.downgrade, config, "base")
    await db_engine.dispose()
    assert await _tables(db_engine) == {"alembic_version"}

    await asyncio.to_thread(command.upgrade, config, "head")
    await db_engine.dispose()
    assert {"worlds", "events", "world_event_seq", "model_calls"} <= await _tables(db_engine)
    await asyncio.to_thread(command.check, config)


async def test_concurrent_upgrades_wait_for_each_other(
    database: ClonedDatabase, db_engine: AsyncEngine
) -> None:
    """Render runs `alembic upgrade head` as the pre-deploy of web and of the worker,
    independently: two runs on one database must not both apply the same migration."""
    config = _config(database)
    await asyncio.to_thread(command.downgrade, config, "base")
    await db_engine.dispose()
    env = {**os.environ, "DATABASE_URL": database.dsn, "DATABASE_DIRECT_URL": database.dsn}

    async def pre_deploy() -> tuple[int | None, str]:
        process = await asyncio.create_subprocess_exec(
            *(sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic.ini")),
            *("upgrade", "head"),
            cwd=REPO_ROOT,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        return process.returncode, output.decode(errors="replace")

    for code, output in await asyncio.gather(pre_deploy(), pre_deploy()):
        assert code == 0, output[-2000:]
    await asyncio.to_thread(command.check, config)
