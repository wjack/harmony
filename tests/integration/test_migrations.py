"""The migrations round-trip and match the models (`alembic check`)."""

from __future__ import annotations

import asyncio
import os
import sys
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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


async def test_round_2_migrations_round_trip_from_0002(
    database: ClonedDatabase, db_engine: AsyncEngine
) -> None:
    config = _config(database)
    added = {"steers", "rep_guidance", "thread_runs"}
    await asyncio.to_thread(command.downgrade, config, "0002")
    await db_engine.dispose()
    assert not added & await _tables(db_engine)

    await asyncio.to_thread(command.upgrade, config, "head")
    await db_engine.dispose()
    assert added <= await _tables(db_engine)
    await asyncio.to_thread(command.check, config)


async def test_one_guidance_row_per_steer_and_run_1_was_never_reopened(
    db_engine: AsyncEngine,
) -> None:
    # Foreign keys are deferred to commit, so bare ids reach the CHECKs and UNIQUEs.
    world, thread, org, member, steer = (uuid4() for _ in range(5))
    ids = {"world": world, "thread": thread, "org": org, "member": member, "steer": steer}
    guidance = text(
        "INSERT INTO rep_guidance (world_id, id, thread_id, org_id, side, version, text,"
        " steer_id, created_by_member_id)"
        " VALUES (:world, :id, :thread, :org, 'responder', :version, '', :steer, :member)"
    )
    async with db_engine.connect() as conn:
        await conn.execute(guidance, {**ids, "id": uuid4(), "version": 1})
        with pytest.raises(IntegrityError, match="uq_rep_guidance_world_id_steer_id"):
            await conn.execute(guidance, {**ids, "id": uuid4(), "version": 2})
        await conn.rollback()

        with pytest.raises(IntegrityError, match="ck_thread_runs_reopened"):
            await conn.execute(
                text(
                    "INSERT INTO thread_runs (world_id, thread_id, run, started_at, max_turns,"
                    " steer_id) VALUES (:world, :thread, 1, now(), 12, :steer)"
                ),
                ids,
            )
        await conn.rollback()


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
