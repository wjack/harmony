"""`harmony live pause|resume`: the switch written through the database, as Render Shell
runs it when the API is down (LIVE_DEMO_PLAN 3.4)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from harmony import cli_live
from harmony.cli import app
from harmony.config import Settings
from harmony.db.models import LiveControls
from harmony.llm.budget_pg import LIVE_SWITCH_KEY

runner = CliRunner()


async def _switch(sm: async_sessionmaker[AsyncSession]) -> tuple[bool, datetime | None]:
    async with sm() as session:
        row = await session.get(LiveControls, LIVE_SWITCH_KEY, populate_existing=True)
        assert row is not None
        return row.paused, row.provider_blocked_until


async def test_pause_and_resume_write_the_switch_through_the_database(
    app_settings: Settings,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_live, "get_settings", lambda: app_settings)

    # `asyncio.run` inside the command needs a thread without a running loop.
    paused = await asyncio.to_thread(runner.invoke, app, ["live", "pause"])
    assert paused.exit_code == 0, paused.output
    assert paused.output.splitlines()[0] == "live models paused"
    assert await _switch(db_sessionmaker) == (True, None)

    async with db_sessionmaker() as session, session.begin():
        blocked = datetime.now(UTC) + timedelta(hours=1)
        await session.execute(
            update(LiveControls)
            .where(LiveControls.key == LIVE_SWITCH_KEY)
            .values(provider_blocked_until=blocked)
        )
    resumed = await asyncio.to_thread(runner.invoke, app, ["live", "resume"])
    assert resumed.exit_code == 0, resumed.output
    assert resumed.output.splitlines()[0] == "live models on"
    # Resuming clears a provider block, as `POST /api/admin/live` does.
    assert await _switch(db_sessionmaker) == (False, None)
