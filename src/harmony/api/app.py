"""The FastAPI application: `/api` (REST + SSE) and the built SPA everywhere else.

Run with `uvicorn harmony.api.app:create_app --factory`. Same origin for API and SPA
means no CORS and cookies just work (ARCHITECTURE.md §3); it also lets every
state-changing `/api` request be refused unless the browser says it is same-origin
(`SameOriginWrites`). The lifespan configures the process's logging (`harmony.logs`;
uvicorn sets up only its own loggers) and runs its background pieces: the LISTEN bus
(§8), the Temporal client and retirer, the hosted budget rows
(`budget_pg.ensure_hosted_rows`), and the startup check that retires worlds whose
workflows Temporal no longer has (§13).

The passcode gate (`api/gate.py`) is the outermost middleware, with `BodyLimit` just
inside it, so nothing reaches a route, the SPA or `/assets` locked. While it is on
(`ACCESS_CODE` set), every SPA file is cached `private` and its HTML varies on the
cookie, so no shared cache serves a gated page. The docs routes are off in prod.

There is deliberately no GZip middleware: it buffers responses, which breaks SSE.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.staticfiles import StaticFiles
from starlette.types import Scope
from temporalio.client import Client

from harmony.api import gate
from harmony.api.deps import AppServices
from harmony.api.gate import BodyLimit, PasscodeGate
from harmony.api.routes import (
    admin,
    budget,
    cards,
    conversations,
    demo,
    escalations,
    events,
    health,
    inbox,
    members,
    network,
    orgs,
    requests,
    session,
    threads,
    workspace,
    worlds,
)
from harmony.api.routes.problems import ApiProblemError, problem_handler, validation_handler
from harmony.api.same_origin import SameOriginWrites
from harmony.config import Settings, get_settings
from harmony.db.engine import create_sessionmaker, engine_from_settings
from harmony.events.bus import EventBus
from harmony.events.pg_bus import PgEventBus
from harmony.events.schemas import EventEnvelope
from harmony.llm.budget_pg import ensure_hosted_rows
from harmony.logs import configure_logging
from harmony.temporal.client import connect_client
from harmony.worlds.retirer import Retirer, SyncRetirer
from harmony.worlds.retirer_temporal import (
    FallbackRetirer,
    TemporalRetirer,
    retire_orphaned_worlds,
)

logger = logging.getLogger(__name__)

API_PREFIX = "/api"
P1_P2_ROUTERS = (
    conversations,
    cards,
    requests,
    inbox,
    escalations,
    workspace,
    orgs,
    network,
    demo,
)
# The live phase (docs/P1_P2_CONTRACT.md, "Live phase"); errors are `ApiProblem`s too.
LIVE_ROUTERS = (gate, budget, admin)
_IMMUTABLE = "public, max-age=31536000, immutable"
_NO_CACHE = "no-cache"
# While the gate is on: no shared cache may keep what only an unlocked visitor sees.
_GATED_IMMUTABLE = "private, max-age=31536000, immutable"
_GATED_NO_CACHE = "private, no-cache"

_PLACEHOLDER_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Harmony</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 4rem auto;">
<h1>Harmony API is running</h1>
<p>The web build is absent. Run <code>pnpm -C web build</code>, or use the Vite dev
server (<code>make dev</code>), which proxies <code>/api</code> here.</p>
<p><a href="/api/docs">API docs</a></p>
</body></html>
"""


class HarmonyApp(FastAPI):
    """FastAPI plus the SSE event schemas in the OpenAPI document.

    `GET /api/events` streams `EventEnvelope`s, which no route declares as a response
    model, so they are added to `components.schemas` here for `openapi-typescript`.
    """

    def openapi(self) -> dict[str, Any]:
        if self.openapi_schema is None:
            _add_event_schemas(super().openapi())
        return super().openapi()


def _add_event_schemas(schema: dict[str, Any]) -> None:
    event_schema = TypeAdapter(EventEnvelope).json_schema(
        mode="serialization", ref_template="#/components/schemas/{model}"
    )
    defs: dict[str, Any] = event_schema.pop("$defs", {})
    components: dict[str, Any] = schema.setdefault("components", {}).setdefault("schemas", {})
    for name, definition in defs.items():
        components.setdefault(name, definition)
    components.setdefault("EventEnvelope", event_schema)


class _ImmutableStaticFiles(StaticFiles):
    """Vite's hashed assets never change under the same name."""

    def __init__(self, *, directory: Path, cache_control: str) -> None:
        super().__init__(directory=directory)
        self._cache_control = cache_control

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = self._cache_control
        return response


def _mount_spa(app: FastAPI, dist: Path, *, gated: bool) -> None:
    assets = dist / "assets"
    if assets.is_dir():
        app.mount(
            "/assets",
            _ImmutableStaticFiles(
                directory=assets, cache_control=_GATED_IMMUTABLE if gated else _IMMUTABLE
            ),
            name="assets",
        )

    root = dist.resolve()
    headers = (
        {"Cache-Control": _GATED_NO_CACHE, "Vary": "Cookie"}
        if gated
        else {"Cache-Control": _NO_CACHE}
    )

    # Registered last, so every /api route and the /assets mount match first. Deep
    # links such as /requests/:id get index.html and survive a refresh.
    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa(path: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        if path == "api" or path.startswith(("api/", "assets/")):
            raise HTTPException(status_code=404)
        if path:
            candidate = (root / path).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                return FileResponse(candidate, headers=headers)
        index = root / "index.html"
        if index.is_file():
            return FileResponse(index, headers=headers)
        return HTMLResponse(_PLACEHOLDER_HTML, headers=headers)


async def _start_services(services: AppServices, stack: AsyncExitStack) -> AppServices:
    """Start what the API process runs beside its routes; `stack` stops it all."""
    settings = services.settings
    bus = services.event_bus or PgEventBus.from_settings(settings)
    # Fails fast on a bad DATABASE_DIRECT_URL rather than degrading to polling.
    await bus.start()
    stack.push_async_callback(bus.aclose)
    await ensure_hosted_rows(services.sessionmaker, settings)

    # Lazy: the API starts (and serves everything but retirement) while Temporal is down.
    identity = f"{os.getpid()}@{socket.gethostname()} harmony-api {settings.build_sha[:12]}"
    client = services.temporal or await connect_client(settings, lazy=True, identity=identity)
    retirer = services.retirer or FallbackRetirer(
        TemporalRetirer(client, services.sessionmaker), SyncRetirer(services.sessionmaker)
    )

    # After a Temporal reset, live worlds point at workflows that no longer exist (§13).
    # The check runs in the background so a slow Temporal never delays startup.
    orphans = asyncio.create_task(
        retire_orphaned_worlds(client, services.sessionmaker, retirer),
        name="retire-orphaned-worlds",
    )
    orphans.add_done_callback(_log_orphan_check)
    stack.push_async_callback(_cancel, orphans)
    return replace(services, event_bus=bus, temporal=client, retirer=retirer)


def _log_orphan_check(task: asyncio.Task[list[UUID]]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("the startup check for orphaned worlds failed", exc_info=exc)
    elif retired := task.result():
        logger.info("retired %d orphaned worlds: %s", len(retired), ", ".join(map(str, retired)))


async def _cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def create_app(
    settings: Settings | None = None,
    *,
    engine: AsyncEngine | None = None,
    event_bus: EventBus | None = None,
    temporal: Client | None = None,
    retirer: Retirer | None = None,
) -> FastAPI:
    """Build the app. Nothing connects until the first request (or the lifespan).

    The lifespan (uvicorn runs it) starts the LISTEN bus, builds a lazy Temporal
    client and the `FallbackRetirer`, and checks for orphaned worlds in the background;
    each piece passed in here is used instead of building one. Tests that drive the app
    through `httpx.ASGITransport` skip the lifespan: they pass a started `event_bus` if
    they need `/api/events`, and worlds then retire inline (`SyncRetirer`).

    An engine passed in is owned by the caller and not disposed at shutdown.
    """
    settings = settings or get_settings()
    owns_engine = engine is None
    engine = engine or engine_from_settings(settings, application_name="harmony-api")
    services = AppServices(
        settings=settings,
        engine=engine,
        sessionmaker=create_sessionmaker(engine),
        event_bus=event_bus,
        temporal=temporal,
        retirer=retirer,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        configure_logging()
        async with AsyncExitStack() as stack:
            if owns_engine:
                stack.push_async_callback(services.engine.dispose)
            app.state.services = await _start_services(app.state.services, stack)
            yield

    # `make gen-api` builds the document in process, so prod needs no docs routes.
    docs = settings.app_env != "prod"
    app = HarmonyApp(
        title="Harmony",
        version=settings.build_sha,
        openapi_url=f"{API_PREFIX}/openapi.json" if docs else None,
        docs_url=f"{API_PREFIX}/docs" if docs else None,
        swagger_ui_oauth2_redirect_url=f"{API_PREFIX}/docs/oauth2-redirect" if docs else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.services = services
    # A third-party page must not be able to POST here (login CSRF; `api/same_origin.py`).
    app.add_middleware(SameOriginWrites, prefix=API_PREFIX)
    # Each `add_middleware` wraps the ones before it: the gate ends up outermost.
    app.add_middleware(BodyLimit)
    app.add_middleware(PasscodeGate, settings=settings)

    for module in (health, session, worlds, members, threads, events):
        app.include_router(module.router, prefix=API_PREFIX)
    # P1–P2 (docs/P1_P2_CONTRACT.md); their errors are typed `ApiProblem`s.
    app.add_exception_handler(ApiProblemError, problem_handler)
    app.add_exception_handler(RequestValidationError, validation_handler)
    for module in (*P1_P2_ROUTERS, *LIVE_ROUTERS):
        app.include_router(module.router, prefix=API_PREFIX)

    _mount_spa(app, settings.web_dist_dir, gated=settings.access_code is not None)
    return app
