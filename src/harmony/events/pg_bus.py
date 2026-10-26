"""The Postgres `EventBus`: one dedicated LISTEN connection per API process (§8).

The connection is a plain asyncpg connection on DATABASE_DIRECT_URL, outside the
SQLAlchemy pool, because LISTEN does not survive a transaction-mode pooler. It listens
on `events`, `stream` and `session`; every notification is only a wake-up hint for
`SubscriberHub`, so losing one costs latency, never an event:

- when the connection drops (asyncpg's termination listener, or a failed periodic
  health check for half-open sockets), it reconnects with backoff and then wakes every
  subscriber to re-read past its cursor;
- each subscription also polls whenever `poll_interval_s` passes without a read.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import ValidationError

from harmony.config import Settings
from harmony.events.bus import (
    EVENTS_CHANNEL,
    POLL_INTERVAL_S,
    SESSION_CHANNEL,
    STREAM_CHANNEL,
    StreamMessage,
    SubscriberHub,
    Subscription,
)

logger = logging.getLogger(__name__)

DEFAULT_RECONNECT_DELAYS_S: tuple[float, ...] = (0.1, 0.5, 1.0, 2.0, 5.0)
HEALTH_CHECK_INTERVAL_S = 30.0
_HEALTH_CHECK_TIMEOUT_S = 5.0

type _Connection = asyncpg.Connection[Any]


@dataclass(frozen=True, slots=True)
class _Listening:
    connection: _Connection
    lost: asyncio.Event


def parse_events_payload(payload: str) -> tuple[UUID, int] | None:
    """`'<world_id>:<seq>'` from the `events` channel, or None if malformed."""
    world, _, seq = payload.partition(":")
    try:
        return UUID(world), int(seq)
    except ValueError:
        return None


class PgEventBus:
    """`EventBus` over Postgres LISTEN/NOTIFY. Build one per API process."""

    def __init__(
        self,
        dsn: str,
        *,
        poll_interval_s: float = POLL_INTERVAL_S,
        reconnect_delays_s: Sequence[float] = DEFAULT_RECONNECT_DELAYS_S,
        health_check_interval_s: float = HEALTH_CHECK_INTERVAL_S,
        connect_timeout_s: float = 10.0,
        application_name: str = "harmony-listen",
    ) -> None:
        if not reconnect_delays_s:
            raise ValueError("reconnect_delays_s must not be empty")
        self._dsn = dsn
        self._hub = SubscriberHub(poll_interval_s=poll_interval_s)
        self._reconnect_delays_s = tuple(reconnect_delays_s)
        self._health_check_interval_s = health_check_interval_s
        self._connect_timeout_s = connect_timeout_s
        self._application_name = application_name
        self._listening: _Listening | None = None
        self._supervisor: asyncio.Task[None] | None = None

    @classmethod
    def from_settings(
        cls, settings: Settings, *, poll_interval_s: float = POLL_INTERVAL_S
    ) -> PgEventBus:
        """The API process's bus, on DATABASE_DIRECT_URL."""
        return cls(settings.asyncpg_dsn(direct=True), poll_interval_s=poll_interval_s)

    @property
    def listener_pid(self) -> int | None:
        """The LISTEN connection's server PID while connected (diagnostics and tests)."""
        listening = self._listening
        if listening is None or listening.lost.is_set():
            return None
        return listening.connection.get_server_pid()

    async def start(self) -> None:
        """Connect and LISTEN. The first connection must succeed, so a misconfigured
        DATABASE_DIRECT_URL fails startup instead of degrading silently to polling."""
        if self._supervisor is not None:
            raise RuntimeError("the event bus is already started")
        listening = await self._connect()
        self._supervisor = asyncio.create_task(self._supervise(listening), name="harmony-listen")

    async def aclose(self) -> None:
        self._hub.close()
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            supervisor.cancel()
            try:
                await supervisor
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
        listening, self._listening = self._listening, None
        if listening is not None:
            await _close(listening.connection)

    @asynccontextmanager
    async def subscribe(self, world_id: UUID, session_id: UUID) -> AsyncGenerator[Subscription]:
        async with self._hub.subscribe(world_id, session_id) as subscription:
            yield subscription

    # --- Connection lifecycle -----------------------------------------------------------

    async def _connect(self) -> _Listening:
        connection: _Connection = await asyncpg.connect(
            self._dsn,
            timeout=self._connect_timeout_s,
            server_settings={"application_name": self._application_name},
        )
        lost = asyncio.Event()
        connection.add_termination_listener(lambda _connection: lost.set())
        try:
            await connection.add_listener(EVENTS_CHANNEL, self._on_events)
            await connection.add_listener(STREAM_CHANNEL, self._on_stream)
            await connection.add_listener(SESSION_CHANNEL, self._on_session)
        except BaseException:
            connection.terminate()
            raise
        listening = _Listening(connection, lost)
        self._listening = listening
        return listening

    async def _supervise(self, listening: _Listening) -> None:
        while True:
            await self._hold(listening)
            self._listening = None
            listening.connection.terminate()
            logger.warning("LISTEN connection lost; reconnecting")
            listening = await self._reconnect()
            # LISTEN is active again; notifications sent while it was down are gone,
            # so every subscriber re-reads past its cursor.
            self._hub.wake_all()
            logger.info("LISTEN connection re-established")

    async def _hold(self, listening: _Listening) -> None:
        """Return once the connection is gone or stops answering."""
        while not listening.lost.is_set():
            try:
                async with asyncio.timeout(self._health_check_interval_s):
                    await listening.lost.wait()
            except TimeoutError:
                if not await _healthy(listening.connection):
                    return

    async def _reconnect(self) -> _Listening:
        attempt = 0
        while True:
            delay = self._reconnect_delays_s[min(attempt, len(self._reconnect_delays_s) - 1)]
            attempt += 1
            # Jitter, so several API processes don't reconnect in lockstep.
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            try:
                return await self._connect()
            except Exception as exc:
                logger.warning("LISTEN reconnect attempt %d failed: %r", attempt, exc)

    # --- Notifications (sync callbacks on the event loop) -------------------------------

    def _on_events(self, _connection: object, _pid: int, _channel: str, payload: object) -> None:
        parsed = parse_events_payload(payload) if isinstance(payload, str) else None
        if parsed is None:
            logger.warning("ignoring malformed %s payload %r", EVENTS_CHANNEL, payload)
            return
        self._hub.events_committed(*parsed)

    def _on_stream(self, _connection: object, _pid: int, _channel: str, payload: object) -> None:
        try:
            message = StreamMessage.model_validate_json(str(payload))
        except ValidationError:
            logger.warning("ignoring malformed %s payload", STREAM_CHANNEL)
            return
        self._hub.stream_received(message)

    def _on_session(self, _connection: object, _pid: int, _channel: str, payload: object) -> None:
        try:
            session_id = UUID(str(payload))
        except ValueError:
            logger.warning("ignoring malformed %s payload %r", SESSION_CHANNEL, payload)
            return
        self._hub.session_poked(session_id)


async def _healthy(connection: _Connection) -> bool:
    try:
        async with asyncio.timeout(_HEALTH_CHECK_TIMEOUT_S):
            await connection.execute("SELECT 1")
    except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        logger.warning("LISTEN connection health check failed: %r", exc)
        return False
    return True


async def _close(connection: _Connection) -> None:
    if connection.is_closed():
        return
    try:
        await connection.close(timeout=2)
    except Exception:
        connection.terminate()
