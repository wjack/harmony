"""Process-wide FastAPI dependencies: settings, database, event bus, worlds; and the
helpers routes use to answer with a snapshot cursor.

`create_app` builds one `AppServices` and stores it on `app.state.services`; routes
reach it only through these dependencies, so tests swap pieces by passing them to
`create_app(...)` rather than patching globals. Identity dependencies (who is acting)
live in `harmony.auth`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Final, cast
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.client import Client

from harmony.config import Settings
from harmony.events.bus import EventBus
from harmony.events.cursor import current_cursor
from harmony.llm.budget_pg import HostedCaps
from harmony.worlds.requests_temporal import TemporalRequestStarter
from harmony.worlds.retirer import Retirer, SyncRetirer
from harmony.worlds.service import WorldService

# Every list or snapshot response carries the world seq it is consistent with; the
# client opens its event stream with `?after=<cursor>` (§8).
CURSOR_HEADER: Final = "X-Harmony-Cursor"


@dataclass(frozen=True, slots=True)
class AppServices:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    # The app's lifespan fills these in (the LISTEN bus, a lazy Temporal client, and the
    # Temporal retirer with its inline fallback) unless the caller injected them. Tests
    # that drive the app without its lifespan leave them None: `/api/events` then 503s
    # and worlds retire inline through `SyncRetirer`.
    event_bus: EventBus | None = None
    temporal: Client | None = None
    retirer: Retirer | None = None


def get_services(request: Request) -> AppServices:
    return cast(AppServices, request.app.state.services)


ServicesDep = Annotated[AppServices, Depends(get_services)]


def get_app_settings(services: ServicesDep) -> Settings:
    return services.settings


def get_sessionmaker(services: ServicesDep) -> async_sessionmaker[AsyncSession]:
    return services.sessionmaker


async def get_db(services: ServicesDep) -> AsyncIterator[AsyncSession]:
    """A session for read paths. Writes that emit events use the UnitOfWork instead."""
    async with services.sessionmaker() as session:
        yield session


def get_event_bus(services: ServicesDep) -> EventBus:
    if services.event_bus is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "event bus not configured")
    return services.event_bus


def get_retirer(services: ServicesDep) -> Retirer:
    return services.retirer or SyncRetirer(services.sessionmaker)


def get_world_service(services: ServicesDep) -> WorldService:
    temporal = services.temporal
    return WorldService(
        services.sessionmaker,
        build_sha=services.settings.build_sha,
        retirer=get_retirer(services),
        requests=(
            TemporalRequestStarter(temporal, services.sessionmaker)
            if temporal is not None
            else None
        ),
        caps=HostedCaps.from_settings(services.settings),
    )


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
SessionmakerDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]
DbDep = Annotated[AsyncSession, Depends(get_db)]
EventBusDep = Annotated[EventBus, Depends(get_event_bus)]
WorldServiceDep = Annotated[WorldService, Depends(get_world_service)]


# --- Snapshots and cursors ------------------------------------------------------------


@asynccontextmanager
async def snapshot_session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    """A REPEATABLE READ transaction: the cursor and the data it accompanies come from
    one database snapshot, whatever order they are read in."""
    async with sessionmaker() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        yield session


async def read_cursor(session: AsyncSession, world_id: UUID) -> int:
    """The world's snapshot cursor; 401 if the world's rows are gone (retired and
    deleted while the request ran), which sends the client back to `start`."""
    try:
        return await current_cursor(session, world_id)
    except LookupError:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "this world is gone; POST /api/session/start"
        ) from None


def set_cursor(response: Response, cursor: int) -> None:
    response.headers[CURSOR_HEADER] = str(cursor)


# --- OpenAPI ----------------------------------------------------------------------------


class ErrorBody(BaseModel):
    """FastAPI's error body: `{"detail": "..."}`."""

    detail: str


def documented(*codes: int, cursor: bool = False) -> dict[int | str, dict[str, Any]]:
    """`responses=` for a route: its error codes, and the cursor header on 200."""
    responses: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in codes}
    if cursor:
        responses[200] = {
            "headers": {
                CURSOR_HEADER: {
                    "description": "The world seq this response is consistent with; "
                    "open the event stream with ?after=<cursor>.",
                    "schema": {"type": "string"},
                }
            }
        }
    return responses
