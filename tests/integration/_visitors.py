"""Test helpers: drive the API as a browser would, one cookie jar per visitor."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import httpx
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from harmony.api.app import create_app
from harmony.api.deps import CURSOR_HEADER
from harmony.config import Settings
from harmony.seeds.ids import SeedKind, seed_id

ADMIN_SECRET = "test-admin-secret"
# The passcode of `gated_app`.
PASSCODE = "integration passcode"

type Json = dict[str, Any]


def demo_id(kind: SeedKind, key: str) -> UUID:
    """A harmony-demo seed ID: the same in every world of that scenario."""
    return seed_id("harmony-demo", kind, key)


HISTORY = demo_id("thread", "henderson-northlake-test-ct")
RAJ_THREAD = demo_id("thread", "ridgeline-northlake-bracket-ct")


def build_app(settings: Settings, engine: AsyncEngine, **overrides: Any) -> FastAPI:
    """The app on the test database, with a known admin secret."""
    return create_app(
        settings.model_copy(update={"admin_secret": SecretStr(ADMIN_SECRET), **overrides}),
        engine=engine,
    )


def gated_app(settings: Settings, engine: AsyncEngine, **overrides: Any) -> FastAPI:
    """`build_app` with the passcode gate on, keyed on `X-Forwarded-For`: a fresh app,
    so fresh limits."""
    gate = {"access_code": SecretStr(PASSCODE), "client_ip_header": "x-forwarded-for"}
    return build_app(settings, engine, **(gate | overrides))


@dataclass
class Visitor:
    """One browser: its own cookie jar against one app."""

    client: httpx.AsyncClient

    async def request(
        self, method: str, path: str, *, expect: int = 200, **kwargs: Any
    ) -> httpx.Response:
        response = await self.client.request(method, f"/api{path}", **kwargs)
        assert response.status_code == expect, (method, path, response.status_code, response.text)
        return response

    async def json(self, method: str, path: str, *, expect: int = 200, **kwargs: Any) -> Any:
        return (await self.request(method, path, expect=expect, **kwargs)).json()

    async def unlock(self, passcode: str = PASSCODE) -> None:
        await self.request("POST", "/gate", json={"passcode": passcode})

    async def start(self, **body: Any) -> Json:
        return cast(Json, await self.json("POST", "/session/start", json=body or None))

    async def session(self, **params: str) -> Json:
        return cast(Json, await self.json("GET", "/session", params=params))

    async def member_ids(self) -> dict[str, UUID]:
        directory = cast(Json, await self.json("GET", "/members"))
        members = directory["featured"] + directory["all"]
        return {member["slug"]: UUID(member["id"]) for member in members}

    async def act_as(self, slug: str) -> Json:
        member_id = (await self.member_ids())[slug]
        return cast(
            Json, await self.json("POST", "/session/act-as", json={"member_id": str(member_id)})
        )

    async def inbox(self) -> dict[UUID, Json]:
        threads = cast(list[Json], await self.json("GET", "/threads"))
        return {UUID(thread["id"]): thread for thread in threads}

    def cookies(self) -> httpx.Cookies:
        return self.client.cookies


def cursor_of(response: httpx.Response) -> int:
    return int(response.headers[CURSOR_HEADER])


@asynccontextmanager
async def visitor(
    app: FastAPI, cookies: httpx.Cookies | None = None, *, headers: dict[str, str] | None = None
) -> AsyncIterator[Visitor]:
    """`headers` go on every request: `X-Forwarded-For` makes a known client key."""
    # https, so the session cookie is sent whether or not the settings make it Secure.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://harmony.test", cookies=cookies, headers=headers
    ) as client:
        yield Visitor(client)
