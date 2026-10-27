"""Section 5 fixes on the API: a pause or resume reaches open tabs at once, and a 422
doesn't echo what was sent. Nothing calls a model."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from harmony.config import Settings
from harmony.db.models import Event
from tests.integration._visitors import ADMIN_SECRET, build_app, visitor


async def test_pausing_and_resuming_tell_every_live_world(
    app_settings: Settings,
    db_engine: AsyncEngine,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with visitor(build_app(app_settings, db_engine)) as v:
        world_id = UUID((await v.start())["world_id"])
        for enabled in (False, True):
            await v.json(
                "POST",
                "/admin/live",
                json={"enabled": enabled},
                headers={"X-Admin-Secret": ADMIN_SECRET},
            )
    async with db_sessionmaker() as session:
        notices = list(
            await session.scalars(
                select(Event.payload).where(
                    Event.world_id == world_id,
                    Event.type == "demo.notice",
                    Event.scope_kind == "demo",
                )
            )
        )
    assert [notice["kind"] for notice in notices] == ["budget", "budget"]


async def test_a_rejected_message_is_not_echoed_back(
    app_settings: Settings, db_engine: AsyncEngine
) -> None:
    text = "x" * 20_001
    async with visitor(build_app(app_settings, db_engine)) as v:
        await v.start()
        response = await v.request("POST", "/conversations", json={"text": text}, expect=422)
    [error] = response.json()["detail"]
    assert error["type"] == "string_too_long"
    assert "input" not in error
    assert text not in response.text
