from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from harmony.api.app import create_app
from tests.conftest import settings_without_dotenv


def _app(dist: Path) -> FastAPI:
    return create_app(settings_without_dotenv(app_env="ci", web_dist_dir=dist))


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>spa</title>")
    (dist / "assets" / "app-1a2b3c.js").write_text("console.log(1)")
    (dist / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("outside dist")
    transport = httpx.ASGITransport(app=_app(dist))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app_env"] == "ci"
    assert body["build_sha"]


async def test_deep_links_get_index_without_caching(client: httpx.AsyncClient) -> None:
    for path in ("/", "/requests/123", "/chat/abc"):
        response = await client.get(path)
        assert response.status_code == 200
        assert "<title>spa</title>" in response.text
        assert response.headers["cache-control"] == "no-cache"


async def test_hashed_assets_are_immutable(client: httpx.AsyncClient) -> None:
    response = await client.get("/assets/app-1a2b3c.js")
    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]
    assert (await client.get("/assets/missing.js")).status_code == 404


async def test_root_files_from_the_build_are_served(client: httpx.AsyncClient) -> None:
    response = await client.get("/favicon.svg")
    assert response.text == "<svg/>"


async def test_unknown_api_paths_404_instead_of_serving_the_spa(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"


async def test_paths_cannot_escape_the_build_dir(client: httpx.AsyncClient) -> None:
    response = await client.get("/..%2fsecret.txt")
    assert "outside dist" not in response.text


async def test_placeholder_when_the_build_is_absent(tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=_app(tmp_path / "missing"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/requests/1")
        assert response.status_code == 200
        assert "web build is absent" in response.text
        assert response.headers["cache-control"] == "no-cache"
        assert (await c.get("/assets/app.js")).status_code == 404


async def test_openapi_includes_the_sse_event_schemas(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/api/openapi.json")).json()
    components = schema["components"]["schemas"]
    for name in ("EventEnvelope", "EventScope", "StreamDelta", "EntitySnapshot", "HelloTick"):
        assert name in components, name
    event = components["EventEnvelope"]["properties"]["event"]
    assert event["discriminator"]["propertyName"] == "type"
    assert "/api/healthz" in schema["paths"]
    assert not any(path.startswith("/{") for path in schema["paths"])


def test_every_route_but_the_spa_lives_under_api(tmp_path: Path) -> None:
    app = _app(tmp_path)
    documented = set(app.openapi()["paths"])
    direct = {path for route in app.routes if (path := getattr(route, "path", None))}
    paths = (documented | direct) - {"/{path:path}"}
    assert "/api/healthz" in paths
    assert all(p.startswith("/api/") for p in paths), paths
