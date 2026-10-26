"""The event bus interface: wake-ups for SSE subscribers, and ephemeral stream events.

Durable events are never carried by the bus. They live in the `events` table, and a
bus notification only means "events may exist past your cursor: go and read them".
Replacing Postgres NOTIFY (`events/pg_bus.py`) with Redis pub/sub or Streams means
writing one class against these Protocols, reusing `SubscriberHub` for the in-process
fan-out (ARCHITECTURE.md §8).

Postgres channels and payloads (shared by the UnitOfWork, act-as and `pg_bus`):

- `events`:  `'<world_id>:<seq>'`, issued inside the committing transaction.
- `stream`:  a `StreamMessage` as JSON, from the worker's autocommit notify
  connection (keep it under NOTIFY's 8000-byte payload limit).
- `session`: `'<session_id>'`, issued inside the act-as transaction.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from harmony.events.schemas import StreamEvent

logger = logging.getLogger(__name__)

EVENTS_CHANNEL = "events"
STREAM_CHANNEL = "stream"
SESSION_CHANNEL = "session"

# A subscription gets an `EventsMayExist(reason="poll")` whenever it has not been told
# to read for this long: the safety net for notifications lost while the LISTEN
# connection was down.
POLL_INTERVAL_S = 5.0

# Undelivered stream messages one subscription holds before dropping the oldest. Only a
# client that stops reading gets there, and a lost delta only loses preview text.
MAX_STREAM_BACKLOG = 1000


class StreamMessage(BaseModel):
    """What travels on the `stream` channel: one stream event for one world."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    world_id: UUID
    event: StreamEvent


WakeReason = Literal["notify", "poll", "reconnect"]


@dataclass(frozen=True, slots=True)
class EventsMayExist:
    """Re-read `events` past your cursor. Several hints may be coalesced into one.

    `reason` is `notify` (a commit), `poll` (the periodic safety net) or `reconnect`
    (the LISTEN connection was re-established: notifications may have been lost).
    `seq` is the notifying commit's last seq when known; it is a hint, not a cursor.
    """

    world_id: UUID
    reason: WakeReason
    seq: int | None = None


@dataclass(frozen=True, slots=True)
class StreamReceived:
    """An ephemeral stream event for the subscribed world, unfiltered by scope."""

    message: StreamMessage


@dataclass(frozen=True, slots=True)
class SessionPoked:
    """The subscribed session changed (act-as). Re-read its epoch; close if it moved."""

    session_id: UUID


Wake = EventsMayExist | StreamReceived | SessionPoked


class SubscriptionClosed(Exception):
    """The bus shut down (or was never usable): the stream should end."""


class Subscription(Protocol):
    """One SSE stream's view of the bus, for one (world, session)."""

    @property
    def world_id(self) -> UUID: ...

    @property
    def session_id(self) -> UUID: ...

    async def get(self) -> Wake:
        """Wait for the next wake-up.

        Delivers `EventsMayExist` for the subscribed world (on notify, after every
        reconnect, and as a poll whenever `POLL_INTERVAL_S` passes without one),
        `StreamReceived` for the world's stream events in arrival order, and
        `SessionPoked` for the subscribed session. Raises `SubscriptionClosed` once the
        bus is closed.
        """
        ...


class EventBus(Protocol):
    """The subscribe side: one per API process, with one LISTEN connection."""

    async def start(self) -> None:
        """Connect and LISTEN on all three channels. Called once from the app lifespan."""
        ...

    async def aclose(self) -> None:
        """Stop listening and release every subscription. Called at shutdown."""
        ...

    def subscribe(
        self, world_id: UUID, session_id: UUID
    ) -> AbstractAsyncContextManager[Subscription]:
        """Register a subscriber for the duration of the `async with` block."""
        ...


class StreamPublisher(Protocol):
    """The publish side for ephemeral stream events: one per worker process."""

    async def publish(self, message: StreamMessage) -> None:
        """Best-effort, outside any transaction. A lost message only loses a delta;
        the committed `thread_message` event is the source of truth."""
        ...

    async def aclose(self) -> None: ...


# --- In-process fan-out ---------------------------------------------------------------

_REASON_RANK: dict[WakeReason, int] = {"poll": 0, "notify": 1, "reconnect": 2}


class _HubSubscription:
    """A FIFO of wake-ups with at most one pending read hint and one pending poke.

    A new hint replaces the pending one and moves to the back of the queue, so the read
    it triggers happens after every stream message that arrived before the commit:
    a committed message is never delivered ahead of its own turn's deltas.
    """

    def __init__(
        self,
        world_id: UUID,
        session_id: UUID,
        *,
        poll_interval_s: float,
        max_backlog: int,
    ) -> None:
        self._world_id = world_id
        self._session_id = session_id
        self._poll_interval_s = poll_interval_s
        self._max_backlog = max_backlog
        self._queue: deque[Wake] = deque()
        self._hint: EventsMayExist | None = None
        self._poke: SessionPoked | None = None
        self._ready = asyncio.Event()
        self._closed = False
        self._loop = asyncio.get_running_loop()
        self._next_poll = self._loop.time() + poll_interval_s

    @property
    def world_id(self) -> UUID:
        return self._world_id

    @property
    def session_id(self) -> UUID:
        return self._session_id

    async def get(self) -> Wake:
        while True:
            if self._closed:
                raise SubscriptionClosed
            if self._queue:
                return self._pop()
            delay = self._next_poll - self._loop.time()
            if delay <= 0:
                self._restart_poll()
                return EventsMayExist(self._world_id, "poll")
            self._ready.clear()
            try:
                async with asyncio.timeout(delay):
                    await self._ready.wait()
            except TimeoutError:
                pass

    def events_may_exist(self, reason: WakeReason, seq: int | None) -> None:
        previous = self._hint
        if previous is not None:
            self._remove(previous)
            if _REASON_RANK[previous.reason] > _REASON_RANK[reason]:
                reason = previous.reason
            if previous.seq is not None and (seq is None or previous.seq > seq):
                seq = previous.seq
        self._hint = EventsMayExist(self._world_id, reason, seq)
        self._append(self._hint)

    def session_poked(self) -> None:
        if self._poke is not None:
            self._remove(self._poke)
        self._poke = SessionPoked(self._session_id)
        self._append(self._poke)

    def stream_received(self, message: StreamMessage) -> None:
        self._append(StreamReceived(message))
        if len(self._queue) > self._max_backlog:
            self._drop_oldest_stream_message()

    def close(self) -> None:
        self._closed = True
        self._ready.set()

    def _pop(self) -> Wake:
        wake = self._queue.popleft()
        if wake is self._hint:
            self._hint = None
            # The subscriber is about to read, so the safety-net poll restarts now.
            self._restart_poll()
        elif wake is self._poke:
            self._poke = None
        return wake

    def _restart_poll(self) -> None:
        self._next_poll = self._loop.time() + self._poll_interval_s

    def _append(self, wake: Wake) -> None:
        self._queue.append(wake)
        self._ready.set()

    def _remove(self, wake: Wake) -> None:
        for index, queued in enumerate(self._queue):
            if queued is wake:
                del self._queue[index]
                return

    def _drop_oldest_stream_message(self) -> None:
        for index, queued in enumerate(self._queue):
            if isinstance(queued, StreamReceived):
                del self._queue[index]
                logger.debug("stream backlog full for session %s; dropped one", self._session_id)
                return


class SubscriberHub:
    """The in-process half of every `EventBus`: subscriptions and their wake-ups.

    A transport (Postgres LISTEN, Redis, ...) calls `events_committed`,
    `stream_received` and `session_poked` as messages arrive, and `wake_all` after it
    reconnects. Everything here runs on the event loop and never blocks.
    """

    def __init__(
        self,
        *,
        poll_interval_s: float = POLL_INTERVAL_S,
        max_stream_backlog: int = MAX_STREAM_BACKLOG,
    ) -> None:
        self._poll_interval_s = poll_interval_s
        self._max_stream_backlog = max_stream_backlog
        self._by_world: dict[UUID, set[_HubSubscription]] = {}
        self._by_session: dict[UUID, set[_HubSubscription]] = {}
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        return sum(len(subscriptions) for subscriptions in self._by_world.values())

    @asynccontextmanager
    async def subscribe(self, world_id: UUID, session_id: UUID) -> AsyncGenerator[Subscription]:
        if self._closed:
            raise SubscriptionClosed("the event bus is closed")
        subscription = _HubSubscription(
            world_id,
            session_id,
            poll_interval_s=self._poll_interval_s,
            max_backlog=self._max_stream_backlog,
        )
        self._by_world.setdefault(world_id, set()).add(subscription)
        self._by_session.setdefault(session_id, set()).add(subscription)
        try:
            yield subscription
        finally:
            _discard(self._by_world, world_id, subscription)
            _discard(self._by_session, session_id, subscription)

    def events_committed(self, world_id: UUID, seq: int | None = None) -> None:
        for subscription in self._by_world.get(world_id, ()):
            subscription.events_may_exist("notify", seq)

    def stream_received(self, message: StreamMessage) -> None:
        for subscription in self._by_world.get(message.world_id, ()):
            subscription.stream_received(message)

    def session_poked(self, session_id: UUID) -> None:
        for subscription in self._by_session.get(session_id, ()):
            subscription.session_poked()

    def wake_all(self) -> None:
        """Every subscriber re-reads past its cursor (after a reconnect)."""
        for subscriptions in self._by_world.values():
            for subscription in subscriptions:
                subscription.events_may_exist("reconnect", None)

    def close(self) -> None:
        self._closed = True
        for subscriptions in self._by_world.values():
            for subscription in subscriptions:
                subscription.close()


def _discard(
    index: dict[UUID, set[_HubSubscription]], key: UUID, subscription: _HubSubscription
) -> None:
    subscriptions = index.get(key)
    if subscriptions is None:
        return
    subscriptions.discard(subscription)
    if not subscriptions:
        del index[key]
