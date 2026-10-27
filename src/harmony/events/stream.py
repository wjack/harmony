"""Token streaming, worker side: coalesced deltas over NOTIFY (§8 Token streaming).

A turn's text reaches the browser twice: as ephemeral `stream.*` events while the model
writes, and as the committed `thread_message` event afterwards, which is the source of
truth. Stream events are never persisted, so they are best-effort end to end.

- `PgStreamPublisher` is the worker's one dedicated autocommit notify connection.
- `TurnStream` coalesces one representative turn attempt's text into a `stream.delta`
  about every 200 ms, announces the turn with `stream.typing` and repeats it every 5 s
  until the turn ends, and opens a retried attempt with `stream.reset` so clients drop
  the earlier attempt's partial text.
- `ChatStream` does the same for internal Claude in a conversation, as
  `stream.chat_delta`; its `attempt` alone tells clients to drop an earlier attempt.

Who sees what is decided by the API (`events/sse.py`): the producing org's members with
thread access and the network lens get deltas; others with thread access get typing. A
conversation's deltas go to its owner and the lens.

Deltas are only worth sending for turns somebody watches. The caller decides that and
passes `enabled`: a representative turn streams for a featured org
(`org_streams_tokens`), and internal Claude streams in every org's chat, since its owner
is the one watching.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from types import TracebackType
from typing import Any, Self
from uuid import UUID

import asyncpg
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.config import Settings
from harmony.db.models import Org
from harmony.events.bus import STREAM_CHANNEL, StreamMessage, StreamPublisher
from harmony.events.schemas import (
    StreamChatDelta,
    StreamDelta,
    StreamEvent,
    StreamReset,
    StreamTyping,
)

logger = logging.getLogger(__name__)

STREAM_INTERVAL_S = 0.2
# Thinking and tool steps can run far longer than the clients' typing timeout with no
# delta, so a representative turn says it is still typing at this interval (3.5).
TYPING_HEARTBEAT_S = 5.0
# Characters per `stream.delta`. Even if every character took a 6-byte JSON escape, the
# NOTIFY payload stays under Postgres's 8000-byte limit.
MAX_DELTA_CHARS = 1000
_MAX_NOTIFY_BYTES = 7999

type _Connection = asyncpg.Connection[Any]


class PgStreamPublisher:
    """`StreamPublisher` over one autocommit asyncpg connection (one per worker process).

    Publishing never raises: a failed NOTIFY is logged and dropped, and after a failed
    connect the publisher stays quiet for `retry_after_s` instead of stalling every
    delta on a dead database.
    """

    def __init__(
        self,
        dsn: str,
        *,
        connect_timeout_s: float = 5.0,
        retry_after_s: float = 5.0,
        application_name: str = "harmony-stream",
    ) -> None:
        self._dsn = dsn
        self._connect_timeout_s = connect_timeout_s
        self._retry_after_s = retry_after_s
        self._application_name = application_name
        self._connection: _Connection | None = None
        self._retry_at = 0.0
        # One connection runs one statement at a time.
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> PgStreamPublisher:
        """The worker's publisher, on DATABASE_DIRECT_URL (NOTIFY needs no pooler)."""
        return cls(settings.asyncpg_dsn(direct=True))

    async def publish(self, message: StreamMessage) -> None:
        payload = message.model_dump_json()
        if len(payload.encode()) > _MAX_NOTIFY_BYTES:
            logger.warning("dropping a %s message over the NOTIFY size limit", message.event.type)
            return
        async with self._lock:
            connection = await self._connected()
            if connection is None:
                return
            try:
                await connection.execute("SELECT pg_notify($1, $2)", STREAM_CHANNEL, payload)
            except Exception as exc:
                logger.warning("stream publish failed: %r", exc)
                self._connection = None
                connection.terminate()

    async def aclose(self) -> None:
        async with self._lock:
            connection, self._connection = self._connection, None
            if connection is not None and not connection.is_closed():
                try:
                    await connection.close(timeout=2)
                except Exception:
                    connection.terminate()

    async def _connected(self) -> _Connection | None:
        if self._connection is not None and not self._connection.is_closed():
            return self._connection
        loop = asyncio.get_running_loop()
        if loop.time() < self._retry_at:
            return None
        try:
            self._connection = await asyncpg.connect(
                self._dsn,
                timeout=self._connect_timeout_s,
                server_settings={"application_name": self._application_name},
            )
        except Exception as exc:
            logger.warning("stream publisher cannot connect: %r", exc)
            self._connection = None
            self._retry_at = loop.time() + self._retry_after_s
        return self._connection


class _CoalescedStream:
    """Buffers one model call's visible text and publishes it about every `interval_s`.

    `push` is synchronous and only buffers, so it is safe inside a stream callback.
    Entering publishes `_opening()` and starts a background flusher; leaving flushes the
    rest. Disabled streams publish nothing.
    """

    def __init__(
        self,
        publisher: StreamPublisher,
        *,
        world_id: UUID,
        enabled: bool,
        interval_s: float,
    ) -> None:
        self._publisher = publisher
        self._world_id = world_id
        self._enabled = enabled
        self._interval_s = interval_s
        self._buffer: list[str] = []
        self._stopping = asyncio.Event()
        self._flusher: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        if self._enabled:
            for event in self._opening():
                await self._publish(event)
            self._flusher = asyncio.create_task(self._flush_periodically())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stopping.set()
        if self._flusher is not None:
            await self._flusher  # it flushes once more on its way out

    def push(self, text: str) -> None:
        """Buffer model output text; it goes out with the next flush."""
        if self._enabled and text:
            self._buffer.append(text)

    async def flush(self) -> None:
        if not self._buffer:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        for start in range(0, len(text), MAX_DELTA_CHARS):
            await self._publish(self._delta(text[start : start + MAX_DELTA_CHARS]))

    def _opening(self) -> list[StreamEvent]:
        return []

    def _delta(self, text: str) -> StreamEvent:
        raise NotImplementedError

    async def _flush_periodically(self) -> None:
        while not self._stopping.is_set():
            try:
                async with asyncio.timeout(self._interval_s):
                    await self._stopping.wait()
            except TimeoutError:
                await self.flush()
        await self.flush()

    async def _publish(self, event: StreamEvent) -> None:
        await self._publisher.publish(StreamMessage(world_id=self._world_id, event=event))


class TurnStream(_CoalescedStream):
    """One representative turn attempt's token stream. Use it around the model call::

        async with TurnStream(publisher, world_id=..., org_id=..., thread_id=...,
                              turn_id=..., attempt=info.attempt, enabled=watched) as out:
            result = await client.call(request, on_stream_event=...)  # calls out.push(text)

    Entering publishes `stream.reset` (attempt > 1) and `stream.typing`, then
    `stream.typing` again every `typing_every_s` until the block exits.
    """

    def __init__(
        self,
        publisher: StreamPublisher,
        *,
        world_id: UUID,
        org_id: UUID,
        thread_id: UUID,
        turn_id: UUID,
        attempt: int,
        enabled: bool = True,
        interval_s: float = STREAM_INTERVAL_S,
        typing_every_s: float = TYPING_HEARTBEAT_S,
    ) -> None:
        super().__init__(publisher, world_id=world_id, enabled=enabled, interval_s=interval_s)
        self._org_id = org_id
        self._thread_id = thread_id
        self._turn_id = turn_id
        self._attempt = attempt
        self._typing_every_s = typing_every_s
        self._typing: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        if self._enabled:
            self._typing = asyncio.create_task(self._keep_typing())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._typing is not None:
            self._typing.cancel()
            with suppress(asyncio.CancelledError):
                await self._typing
        await super().__aexit__(exc_type, exc, traceback)

    async def _keep_typing(self) -> None:
        while True:
            await asyncio.sleep(self._typing_every_s)
            await self._publish(self._typing_event())

    def _typing_event(self) -> StreamTyping:
        return StreamTyping(org_id=self._org_id, thread_id=self._thread_id, turn_id=self._turn_id)

    def _opening(self) -> list[StreamEvent]:
        typing = self._typing_event()
        if self._attempt == 1:
            return [typing]
        reset = StreamReset(
            org_id=self._org_id,
            thread_id=self._thread_id,
            turn_id=self._turn_id,
            attempt=self._attempt,
        )
        return [reset, typing]

    def _delta(self, text: str) -> StreamEvent:
        return StreamDelta(
            org_id=self._org_id,
            thread_id=self._thread_id,
            turn_id=self._turn_id,
            attempt=self._attempt,
            text=text,
        )


class ChatStream(_CoalescedStream):
    """One internal Claude turn attempt's token stream in a conversation, delivered to
    the conversation's owner (`member_id`) as `stream.chat_delta`."""

    def __init__(
        self,
        publisher: StreamPublisher,
        *,
        world_id: UUID,
        org_id: UUID,
        member_id: UUID,
        conversation_id: UUID,
        turn_id: UUID,
        attempt: int,
        enabled: bool = True,
        interval_s: float = STREAM_INTERVAL_S,
    ) -> None:
        super().__init__(publisher, world_id=world_id, enabled=enabled, interval_s=interval_s)
        self._org_id = org_id
        self._member_id = member_id
        self._conversation_id = conversation_id
        self._turn_id = turn_id
        self._attempt = attempt

    def _delta(self, text: str) -> StreamEvent:
        return StreamChatDelta(
            org_id=self._org_id,
            member_id=self._member_id,
            conversation_id=self._conversation_id,
            turn_id=self._turn_id,
            attempt=self._attempt,
            text=text,
        )


async def org_streams_tokens(session: AsyncSession, world_id: UUID, org_id: UUID) -> bool:
    """P0's "has a subscriber" rule: featured orgs stream their turns (§8, §11.7)."""
    featured = await session.scalar(
        select(Org.featured).where(Org.world_id == world_id, Org.id == org_id)
    )
    return bool(featured)
