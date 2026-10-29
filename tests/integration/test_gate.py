"""The passcode gate over the whole app and its database (LIVE_DEMO_PLAN 3.2, 3.3): the
default-deny route walk, lock, the network world's 404, the world-creation limits, the
client key on new sessions, and the largest legitimate body.

Each test builds a fresh gated app (`gated_app`), so no limit carries over.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from harmony.api.gate_page import PAGE
from harmony.config import Settings
from tests.integration._visitors import (
    ADMIN_SECRET,
    build_app,
    gated_app,
    visitor,
)

type Sessionmaker = async_sessionmaker[AsyncSession]

PASSCODE_REQUIRED = {"detail": "passcode required", "code": "passcode_required"}
CLIENT = "198.51.100.7"
# What passes without the cookie, and what it answers.
OPEN = {("GET", "/api/healthz"): 200, ("HEAD", "/api/healthz"): 200, ("GET", "/api/gate"): 200}


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>spa</title>")
    (dist / "assets" / "x.js").write_text("console.log(1)")
    return dist


@pytest.fixture
def app(app_settings: Settings, db_engine: AsyncEngine, dist: Path) -> FastAPI:
    return gated_app(app_settings, db_engine, web_dist_dir=dist)


def _client_from(ip: str) -> dict[str, str]:
    return {"X-Forwarded-For": f"203.0.113.250, {ip}"}


# --- Default deny -----------------------------------------------------------------------


def _walk(app: FastAPI) -> list[tuple[str, str]]:
    """Every route the app has, as a concrete request, plus paths no route declares.

    FastAPI keeps included routers lazily in `app.routes`, so the API routes come from
    the OpenAPI document; `app.routes` adds the docs, the `/assets` mount and the SPA
    catch-all, the one route left out of the document.
    """
    paths: dict[str, dict[str, object]] = app.openapi()["paths"]
    declared: set[tuple[str, str]] = {
        (method.upper(), template)
        for template, operations in paths.items()
        for method in operations
    }
    for route in app.routes:
        template: str | None = getattr(route, "path", None)
        if template is not None:
            methods: set[str] = set(getattr(route, "methods", None) or {"GET"})
            declared |= {(method, template) for method in methods}
    requests: set[tuple[str, str]] = {
        ("GET", "/assets/x.js"),
        ("GET", "/requests/abc"),
        ("GET", "/api/nope"),
        ("POST", "/api/nope"),
    }
    for method, template in declared:
        path = template.replace("{path:path}", "requests/abc")
        path = re.sub(r"\{[^}]+\}", lambda _: str(uuid4()), path)
        requests.add((method, path))
        if method == "GET":
            requests.add(("HEAD", path))
    return sorted(requests)


async def test_without_the_cookie_every_route_is_refused(app: FastAPI) -> None:
    walk = _walk(app)
    assert {("GET", "/api/events"), ("POST", "/api/worlds/reset")} <= set(walk)
    async with visitor(app) as v:
        for method, path in walk:
            response = await v.client.request(method, path)
            where = (method, path, response.status_code, response.text[:120])
            if (method, path) in OPEN:
                assert response.status_code == OPEN[(method, path)], where
            elif (method, path) == ("POST", "/api/gate"):
                assert response.status_code in (411, 422), where
            elif path == "/api" or path.startswith("/api/") or method not in ("GET", "HEAD"):
                assert response.status_code == 401, where
                assert response.headers["content-type"] == "application/json", where
                assert method == "HEAD" or response.json() == PASSCODE_REQUIRED, where
            else:
                assert response.status_code == 401, where
                assert response.headers["content-type"].startswith("text/html"), where
                assert method == "HEAD" or response.text == PAGE, where
            if response.status_code == 401:
                assert response.headers["cache-control"] == "no-store", where
                assert response.headers["x-robots-tag"] == "noindex", where


async def test_admin_routes_pass_with_the_admin_secret_alone(app: FastAPI) -> None:
    admin = [(m, p) for m, p in _walk(app) if p.startswith("/api/admin/") and m != "HEAD"]
    assert {p for _, p in admin} >= {"/api/admin/live", "/api/admin/wipe"}
    async with visitor(app) as v:
        for method, path in admin:
            wrong = await v.client.request(method, path, headers={"X-Admin-Secret": "wrong"})
            assert (wrong.status_code, wrong.json()) == (401, PASSCODE_REQUIRED), (method, path)
            right = await v.client.request(method, path, headers={"X-Admin-Secret": ADMIN_SECRET})
            assert right.status_code != 401, (method, path, right.text)


async def test_unlocked_an_admin_route_still_needs_the_secret(app: FastAPI) -> None:
    """With the gate's cookie, a missing or wrong secret is the route's 403 `not_admin`,
    the wipe included."""
    admin = [(m, p) for m, p in _walk(app) if p.startswith("/api/admin/") and m != "HEAD"]
    async with visitor(app) as v:
        await v.unlock()
        for method, path in admin:
            body = {"enabled": False} if path == "/api/admin/live" else None
            for headers in ({}, {"X-Admin-Secret": "wrong"}):
                refused = await v.client.request(method, path, json=body, headers=headers)
                assert refused.status_code == 403, (method, path, refused.text)
                assert refused.json()["code"] == "not_admin", (method, path)


async def test_lock_sends_the_next_request_back_to_the_gate(app: FastAPI) -> None:
    async with visitor(app) as v:
        await v.unlock()
        no_session = await v.request("GET", "/session", expect=401)
        assert no_session.json().get("code") is None
        await v.start()
        await v.session()
        await v.request("POST", "/gate/lock", expect=204)
        locked = await v.request("GET", "/session", expect=401)
        assert locked.json() == PASSCODE_REQUIRED
        await v.unlock()
        await v.session()


# --- World creation -------------------------------------------------------------------


@pytest.mark.parametrize("gated", [True, False])
async def test_the_network_world_is_not_started(
    app_settings: Settings, db_engine: AsyncEngine, gated: bool
) -> None:
    app = (gated_app if gated else build_app)(app_settings, db_engine)
    async with visitor(app) as v:
        if gated:
            await v.unlock()
        refused = await v.request("POST", "/session/start", params={"world": "network"}, expect=404)
        assert refused.json()["code"] == "not_found"
        await v.start()
        again = await v.request("POST", "/session/start", params={"world": "network"}, expect=404)
        assert again.json()["code"] == "not_found"


async def test_new_sessions_are_not_limited(app: FastAPI) -> None:
    # More than the old five a day from one client key: testers on one network, or one
    # tester's private windows, all get in.
    for _ in range(7):
        async with visitor(app, headers=_client_from(CLIENT)) as v:
            await v.unlock()
            await v.start()


async def test_resets_are_limited_per_session(app: FastAPI) -> None:
    async with (
        visitor(app, headers=_client_from(CLIENT)) as v,
        visitor(app, headers=_client_from(CLIENT)) as neighbour,
    ):
        for each in (v, neighbour):
            await each.unlock()
            await each.start()
        for _ in range(20):
            await v.json("POST", "/worlds/reset")
        refused = await v.request("POST", "/worlds/reset", expect=429)
        assert refused.json()["code"] == "too_many_attempts"
        assert 0 < int(refused.headers["retry-after"]) <= 3600
        # Keyed on the session: the same client key still resets another session.
        await neighbour.json("POST", "/worlds/reset")
        # A refused Reset leaves the session where it was.
        await v.session()


async def test_resets_are_unlimited_without_the_gate(
    app_settings: Settings, db_engine: AsyncEngine
) -> None:
    async with visitor(build_app(app_settings, db_engine), headers=_client_from(CLIENT)) as v:
        for _ in range(6):
            await v.start()
            v.client.cookies.delete("harmony_session")
        await v.start()
        for _ in range(21):
            await v.json("POST", "/worlds/reset")


# --- Bodies -----------------------------------------------------------------------------


async def test_a_20000_character_non_ascii_text_passes_the_body_limit(app: FastAPI) -> None:
    text = "研究" * 10_000
    async with visitor(app) as v:
        await v.unlock()
        await v.start()
        await v.act_as("maya")
        current = await v.json("GET", "/orgs/henderson/harmony")
        body = json.dumps(
            {"policy_version": current["policy_version"], "instructions": text},
            ensure_ascii=True,
        ).encode()
        assert len(body) > 100_000
        updated = await v.json(
            "PUT",
            "/orgs/henderson/harmony",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert updated["instructions"] == text


async def test_the_gate_admits_the_unlocked_visitor_everywhere(app: FastAPI) -> None:
    async with visitor(app) as v:
        await v.unlock()
        spa = await v.client.get("/requests/abc")
        assert (spa.status_code, spa.text) == (200, "<!doctype html><title>spa</title>")
        assert (await v.client.get("/assets/x.js")).status_code == 200
        await v.start()
        assert (await v.client.get("/api/nope")).status_code == 404
