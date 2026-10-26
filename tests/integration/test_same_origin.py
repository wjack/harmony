"""Cross-site writes are refused: a third-party page cannot swap a visitor's session."""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from harmony.auth.session import SESSION_COOKIE
from harmony.config import Settings
from harmony.db.models import World
from tests.integration._visitors import build_app, visitor

type Sessionmaker = async_sessionmaker[AsyncSession]

# What a browser sends when another site auto-submits `<form method=post>` to us. The
# session cookie is SameSite=Lax, so the browser leaves it off this request.
CROSS_SITE_FORM = {
    "content-type": "application/x-www-form-urlencoded",
    "origin": "https://evil.example",
    "sec-fetch-site": "cross-site",
    "sec-fetch-mode": "navigate",
}


@pytest.fixture
def app(app_settings: Settings, db_engine: AsyncEngine) -> FastAPI:
    return build_app(app_settings, db_engine)


async def _world_ids(sm: Sessionmaker) -> set[UUID]:
    async with sm() as session:
        return set(await session.scalars(select(World.id)))


async def test_a_cross_site_form_post_cannot_replace_the_session(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as victim:
        started = await victim.start()
        await victim.act_as("dana")

        # The attacker's form: no cookie, an empty body.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://harmony.test"
        ) as browser:
            refused = await browser.post("/api/session/start", content=b"", headers=CROSS_SITE_FORM)
        assert refused.status_code == 403
        assert SESSION_COOKIE not in refused.headers.get("set-cookie", "")

        after = await victim.session()
    assert after["session_id"] == started["session_id"]
    assert after["member"]["slug"] == "dana"
    assert await _world_ids(db_sessionmaker) == {UUID(started["world_id"])}


@pytest.mark.parametrize(
    "headers",
    [
        {"sec-fetch-site": "cross-site"},
        {"sec-fetch-site": "same-site"},
        {"origin": "https://evil.example"},
        {"origin": "null"},
    ],
    ids=["cross-site", "same-site", "foreign-origin", "opaque-origin"],
)
async def test_every_write_under_api_checks_the_browser_labels(
    app: FastAPI, headers: dict[str, str]
) -> None:
    async with visitor(app) as v:
        await v.start()
        for path in ("/session/start", "/worlds/reset", "/session/act-as"):
            await v.request("POST", path, headers=headers, json={}, expect=403)
        # Reads are never refused (an EventSource or a link may be cross-site).
        await v.request("GET", "/session", headers=headers)


@pytest.mark.parametrize(
    "headers",
    [
        {"sec-fetch-site": "same-origin", "origin": "https://harmony.test"},
        {"origin": "https://harmony.test"},
        {},  # curl, scripts and tests label nothing
    ],
    ids=["same-origin", "own-origin", "unlabelled"],
)
async def test_same_origin_and_unlabelled_writes_pass(
    app: FastAPI, headers: dict[str, str]
) -> None:
    async with visitor(app) as v:
        started = await v.start()
        again = await v.request("POST", "/session/start", headers=headers)
    assert again.json()["session_id"] == started["session_id"]
