"""Gap-free SSE for one subscriber: catch-up reads, the scope filter and framing (§8).

Each stream keeps a cursor, the last seq it has scanned, and:

1. subscribes to the bus *before* its first read, so a commit racing that read is
   either in it or wakes the stream afterwards, and says so with a `: ready` comment
   (which also pushes the response headers through any buffering proxy);
2. on every read hint (notify, poll, reconnect, session poke) reads
   `events WHERE world_id = :w AND seq > :cursor ORDER BY seq LIMIT 500` until drained,
   and sends what the viewer may see as `id: <seq>` + `data: <EventEnvelope JSON>`.
   If the read ends on events the viewer may not see, an id-only frame follows, which
   moves the browser's Last-Event-ID past them without firing `onmessage`;
3. forwards stream events (`stream.*`) as `data:` only. Thread events are filtered
   against a per-stream cache of thread access, so a delta costs a query only for
   a thread it has not seen yet; a conversation's `stream.chat_delta` goes only to the
   conversation's owner (and the lens), with no query at all.

Seqs are allocated in commit order (`db/uow.py`), so reading `seq > cursor` never
skips an event that commits later. The stream ends when its session's epoch moves
(act-as; it delivers up to and including its own `session.member_changed` first),
after `world.retired`, when a poll, reconnect or session poke finds its world no longer
`live` (a wipe that deleted the world's rows, `world.retired` included, before the
stream read them), or when the bus closes. Heartbeats are SSE comments sent by the
response (`EventSourceResponse(ping=...)`), independent of this loop.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterable, AsyncIterator, Callable, Collection, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from pydantic import JsonValue, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sse_starlette import ServerSentEvent

from harmony.auth.access import visible_to
from harmony.db.models import Event, Thread, VisitorSession, World
from harmony.db.models.choices import MemberRole
from harmony.events.bus import (
    EventBus,
    EventsMayExist,
    StreamMessage,
    StreamReceived,
    SubscriptionClosed,
)
from harmony.events.schemas import (
    LENS_HIDDEN_ENTITY_TYPES,
    DemoNotice,
    DurableEvent,
    EntitySnapshot,
    EventEnvelope,
    EventScope,
    ScopeKind,
    SessionMemberChanged,
    StreamChatDelta,
    StreamDelta,
    StreamEvent,
    StreamReset,
    StreamTyping,
    ThreadAccessChanged,
    ThreadStreamEvent,
    WorldRetired,
)

logger = logging.getLogger(__name__)

_DURABLE: TypeAdapter[DurableEvent] = TypeAdapter(DurableEvent)

# The comment a stream sends once it is subscribed: nothing published after it is missed.
READY_COMMENT = "ready"


@dataclass(frozen=True, slots=True)
class SseOptions:
    """Per-stream timings and limits. Tests shorten them; production uses the defaults."""

    heartbeat_s: float = 15.0
    # A client that accepts no bytes for this long is dropped (its socket is stuck).
    send_timeout_s: float = 30.0
    batch_size: int = 500
    # A counterparty sees at most one `stream.typing` per turn this often.
    typing_interval_s: float = 1.0
    # Cached thread access is re-read at least this often, even without a
    # `thread.access_changed` event going by.
    access_ttl_s: float = 30.0


@dataclass(frozen=True, slots=True)
class Viewer:
    """Whom one stream delivers to: the acting member as of the stream's epoch.

    `lens` is the network view (`?lens=network`): a demo lens that sees every event in
    the world, both sides of every thread, but still only its own session's events, and
    never an org's instructions to its own Claude (`lens_hidden`).
    """

    session_id: UUID
    epoch: int
    member_id: UUID
    org_id: UUID
    role: MemberRole
    lens: bool = False


# --- Scope rules (pure) ---------------------------------------------------------------


def may_receive(viewer: Viewer, scope: EventScope, thread_visible: Callable[[UUID], bool]) -> bool:
    """Whether a durable event in `scope` goes to `viewer` (ARCHITECTURE.md §8, §10).

    - session: only that session's streams, the lens included;
    - lens: everything else;
    - world and demo: everyone (demo events carry no entity data);
    - member: only that member;
    - org: members of that org;
    - thread: members of that org who can see the thread, and that org's admins.
    """
    match scope.kind:
        case "session":
            return scope.session_id == viewer.session_id
        case _ if viewer.lens:
            return True
        case "world" | "demo":
            return True
        case "member":
            return scope.member_id == viewer.member_id
        case "org":
            return scope.org_id == viewer.org_id
        case "thread":
            if scope.org_id != viewer.org_id or scope.thread_id is None:
                return False
            return viewer.role == "admin" or thread_visible(scope.thread_id)


def lens_hidden(event: DurableEvent) -> bool:
    """What an org tells its own Claude (`LENS_HIDDEN_ENTITY_TYPES`): the network view
    never receives it, though it sees every other event of both sides."""
    return isinstance(event, EntitySnapshot) and event.entity_type in LENS_HIDDEN_ENTITY_TYPES


def needs_thread_check(viewer: Viewer, scope: EventScope) -> bool:
    """Whether `may_receive` will check thread access for this scope."""
    return (
        not viewer.lens
        and scope.kind == "thread"
        and scope.org_id == viewer.org_id
        and viewer.role != "admin"
    )


def stream_view(
    event: ThreadStreamEvent, viewer: Viewer, *, thread_visible: bool
) -> ThreadStreamEvent | None:
    """What `viewer` receives for a thread's stream event, if anything (§8 Token
    streaming).

    The producing org's members who can see the thread get deltas and resets, as does
    the lens. Everyone else who can see the thread gets only `stream.typing`; the
    counterparty receives text only from the committed `thread_message` event.
    """
    if viewer.lens:
        return event
    if not thread_visible:
        return None
    if event.org_id == viewer.org_id:
        return event
    match event:
        case StreamDelta():
            return StreamTyping(
                org_id=event.org_id, thread_id=event.thread_id, turn_id=event.turn_id
            )
        case StreamTyping():
            return event
        case StreamReset():
            return None


def chat_stream_view(event: StreamChatDelta, viewer: Viewer) -> StreamChatDelta | None:
    """A conversation's deltas go to its owner and the lens only: a chat is private to
    the member who owns it, whatever their org's other members can see."""
    if viewer.lens or event.member_id == viewer.member_id:
        return event
    return None


# --- Thread access --------------------------------------------------------------------


class ThreadAccessCache:
    """Which threads the viewer can see: their org is a party, and they are routed or
    shared in (`thread_access`) or are its admin (§10). The rule is `auth.access`'s
    `visible_to`, the same predicate every REST thread query uses. Cached per stream.

    The cache lets stream deltas be filtered without a query each (§8). An entry is
    dropped when a `thread.access_changed` event for its thread goes by, and expires
    after `ttl_s` regardless. Delivery uses access as of the read, so a member who is
    shared into a thread also receives its earlier thread events still in flight: a
    batch is filtered with the answers `resolve` returned for it, never re-checked
    against the TTL while it is being delivered.
    """

    def __init__(
        self,
        world_id: UUID,
        viewer: Viewer,
        *,
        ttl_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._world_id = world_id
        self._access_viewer = _AccessViewer(world_id, viewer.member_id, viewer.org_id, viewer.role)
        self._ttl_s = ttl_s
        self._clock = clock
        self._cache: dict[UUID, tuple[bool, float]] = {}

    def cached(self, thread_id: UUID) -> bool | None:
        entry = self._cache.get(thread_id)
        if entry is None or entry[1] <= self._clock():
            return None
        return entry[0]

    def forget(self, thread_id: UUID) -> None:
        self._cache.pop(thread_id, None)

    async def resolve(
        self, session: AsyncSession, thread_ids: Collection[UUID]
    ) -> dict[UUID, bool]:
        """Whether the viewer sees each thread in `thread_ids`, as of now: cached answers,
        and one query for the threads that are not cached (which are then cached).

        Use the returned answers for everything read alongside them. Asking the cache
        again later could find an entry that expired in between and read it as "not
        visible", silently dropping an event the viewer may see.
        """
        answers: dict[UUID, bool] = {}
        missing: list[UUID] = []
        for thread_id in thread_ids:
            cached = self.cached(thread_id)
            if cached is None:
                missing.append(thread_id)
            else:
                answers[thread_id] = cached
        if not missing:
            return answers
        visible = set(
            await session.scalars(
                select(Thread.id).where(
                    Thread.world_id == self._world_id,
                    Thread.id.in_(missing),
                    visible_to(self._access_viewer),
                )
            )
        )
        expires = self._clock() + self._ttl_s
        for thread_id in missing:
            answers[thread_id] = thread_id in visible
            self._cache[thread_id] = (answers[thread_id], expires)
        return answers


@dataclass(frozen=True, slots=True)
class _AccessViewer:
    """A stream's viewer as `auth.access` sees one: a member acting in one world."""

    world_id: UUID
    member_id: UUID
    org_id: UUID
    role: MemberRole


# --- One subscriber's stream ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _EventRow:
    seq: int
    created_at: datetime
    # None when the row cannot be read by this build (an event type from a newer build
    # during a deploy overlap); it is skipped, and the cursor moves past it.
    scope: EventScope | None
    event: DurableEvent | None


@dataclass(frozen=True, slots=True)
class _Batch:
    rows: list[_EventRow]
    # Thread access as of the read, for every thread-scoped row that needs it.
    visible: dict[UUID, bool]


@dataclass(slots=True)
class _TypingThrottle:
    interval_s: float
    clock: Callable[[], float] = time.monotonic
    sent: dict[tuple[UUID, UUID], float] = field(default_factory=dict[tuple[UUID, UUID], float])

    def due(self, event: StreamTyping) -> bool:
        now = self.clock()
        key = (event.thread_id, event.turn_id)
        last = self.sent.get(key)
        if last is not None and now - last < self.interval_s:
            return False
        self.sent[key] = now
        if len(self.sent) > 256:
            self.sent = {k: t for k, t in self.sent.items() if now - t < self.interval_s}
        return True


class EventStream:
    """The frames of one SSE connection. Iterate `frames()` once."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        bus: EventBus,
        world_id: UUID,
        viewer: Viewer,
        after: int,
        options: SseOptions | None = None,
    ) -> None:
        options = options or SseOptions()
        self._sessionmaker = sessionmaker
        self._bus = bus
        self._world_id = world_id
        self._viewer = viewer
        self._options = options
        self._cursor = after  # last seq scanned
        self._sent_id = after  # last SSE id sent
        self._done = False
        self._access = ThreadAccessCache(world_id, viewer, ttl_s=options.access_ttl_s)
        self._typing = _TypingThrottle(options.typing_interval_s)

    async def frames(self) -> AsyncIterator[ServerSentEvent]:
        try:
            async with self._bus.subscribe(self._world_id, self._viewer.session_id) as subscription:
                yield ServerSentEvent(comment=READY_COMMENT)
                async for frame in self._catch_up(check_epoch=True):
                    yield frame
                while not self._done:
                    wake = await subscription.get()
                    if isinstance(wake, StreamReceived):
                        frame = await self._stream_frame(wake.message)
                        if frame is not None:
                            yield frame
                        continue
                    # A commit's notify needs no epoch check: an act-as in this world
                    # arrives as our own `session.member_changed` event.
                    check_epoch = not (isinstance(wake, EventsMayExist) and wake.reason == "notify")
                    async for frame in self._catch_up(check_epoch=check_epoch):
                        yield frame
        except SubscriptionClosed:
            return

    async def _catch_up(self, *, check_epoch: bool) -> AsyncIterator[ServerSentEvent]:
        # If the epoch moved, deliver what is committed (stopping at this session's own
        # `session.member_changed` when the act-as was in this world), then end. Either
        # way the old member only receives what that member may see. Likewise if the
        # world is no longer live: its `world.retired` may already be deleted (an inline
        # wipe deletes the rows at once), so it can't be relied on to end the stream.
        final = check_epoch and await self._session_moved_or_world_gone()
        while not self._done:
            batch = await self._read_batch()
            for frame in self._deliver(batch):
                yield frame
            if len(batch.rows) < self._options.batch_size:
                break
        if self._cursor > self._sent_id:
            self._sent_id = self._cursor
            yield ServerSentEvent(id=str(self._cursor))
        if final:
            self._done = True

    async def _read_batch(self) -> _Batch:
        async with self._sessionmaker() as session:
            result = await session.execute(
                select(
                    Event.seq,
                    Event.created_at,
                    Event.scope_kind,
                    Event.org_id,
                    Event.member_id,
                    Event.thread_id,
                    Event.session_id,
                    Event.payload,
                )
                .where(Event.world_id == self._world_id, Event.seq > self._cursor)
                .order_by(Event.seq)
                .limit(self._options.batch_size)
            )
            rows = [
                self._parse(seq, created_at, kind, org_id, member_id, thread_id, session_id, data)
                for seq, created_at, kind, org_id, member_id, thread_id, session_id, data in result
            ]
            for row in rows:
                if isinstance(row.event, ThreadAccessChanged):
                    self._access.forget(row.event.thread_id)
            visible = await self._access.resolve(
                session,
                {
                    row.scope.thread_id
                    for row in rows
                    if row.scope is not None
                    and row.scope.thread_id is not None
                    and needs_thread_check(self._viewer, row.scope)
                },
            )
        return _Batch(rows, visible)

    def _parse(
        self,
        seq: int,
        created_at: datetime,
        kind: ScopeKind,
        org_id: UUID | None,
        member_id: UUID | None,
        thread_id: UUID | None,
        session_id: UUID | None,
        payload: dict[str, JsonValue],
    ) -> _EventRow:
        try:
            scope = EventScope(
                kind=kind,
                org_id=org_id,
                member_id=member_id,
                thread_id=thread_id,
                session_id=session_id,
            )
            event = _DURABLE.validate_python(payload)
        except ValidationError:
            logger.warning(
                "skipping event %s:%d, which this build cannot read", self._world_id, seq
            )
            return _EventRow(seq, created_at, None, None)
        return _EventRow(seq, created_at, scope, event)

    def _deliver(self, batch: _Batch) -> Iterator[ServerSentEvent]:
        # Each yield waits on the client (up to the send timeout), so this loop can
        # outlive cached access answers; it uses the batch's own throughout.
        for row in batch.rows:
            self._cursor = row.seq
            if row.scope is None or row.event is None:
                continue
            if self._visible(row.scope, row.event, batch.visible):
                self._sent_id = row.seq
                envelope = EventEnvelope(
                    world_id=self._world_id,
                    seq=row.seq,
                    created_at=row.created_at,
                    scope=row.scope,
                    event=row.event,
                )
                yield ServerSentEvent(data=envelope.model_dump_json(), id=str(row.seq))
            if self._ends_stream(row.event):
                self._done = True
                return

    def _visible(self, scope: EventScope, event: DurableEvent, visible: dict[UUID, bool]) -> bool:
        # Defence in depth: the UnitOfWork already refuses entity data in demo scope.
        if scope.kind == "demo" and not isinstance(event, DemoNotice):
            return False
        if self._viewer.lens and lens_hidden(event):
            return False
        return may_receive(self._viewer, scope, lambda thread_id: visible.get(thread_id, False))

    def _ends_stream(self, event: DurableEvent) -> bool:
        match event:
            case SessionMemberChanged(session_id=session_id, epoch=epoch):
                return session_id == self._viewer.session_id and epoch != self._viewer.epoch
            case WorldRetired():
                return True
            case _:
                return False

    async def _session_moved_or_world_gone(self) -> bool:
        """Whether the stream should end once it has delivered what is committed: the
        session's epoch moved, or the world is no longer `live` (or no longer exists)."""
        epoch = (
            select(VisitorSession.epoch)
            .where(VisitorSession.id == self._viewer.session_id)
            .scalar_subquery()
        )
        status = select(World.status).where(World.id == self._world_id).scalar_subquery()
        async with self._sessionmaker() as session:
            row = (await session.execute(select(epoch, status))).one()
        return row[0] != self._viewer.epoch or row[1] != "live"

    async def _stream_frame(self, message: StreamMessage) -> ServerSentEvent | None:
        incoming = message.event
        event: StreamEvent | None
        if isinstance(incoming, StreamChatDelta):
            event = chat_stream_view(incoming, self._viewer)
            scope = EventScope.member(incoming.member_id, incoming.org_id)
        else:
            event = incoming
            if not self._viewer.lens:
                visible = await self._thread_visible(incoming.org_id, incoming.thread_id)
                event = stream_view(incoming, self._viewer, thread_visible=visible)
            scope = EventScope.thread(incoming.org_id, incoming.thread_id)
        if event is None or (isinstance(event, StreamTyping) and not self._typing.due(event)):
            return None
        envelope = EventEnvelope(
            world_id=self._world_id, seq=None, created_at=None, scope=scope, event=event
        )
        return ServerSentEvent(data=envelope.model_dump_json())

    async def _thread_visible(self, org_id: UUID, thread_id: UUID) -> bool:
        if self._viewer.role == "admin" and org_id == self._viewer.org_id:
            return True
        cached = self._access.cached(thread_id)
        if cached is not None:
            return cached
        async with self._sessionmaker() as session:
            answers = await self._access.resolve(session, [thread_id])
        return answers.get(thread_id, False)


# --- Decoding (for scripts and tests) -------------------------------------------------


@dataclass(frozen=True, slots=True)
class SseFrame:
    """One message of an SSE stream as the WHATWG parser sees it, comments included.

    `data` is None for an id-only frame (moves Last-Event-ID, fires no event) and for a
    heartbeat comment.
    """

    id: str | None = None
    data: str | None = None
    comment: str | None = None

    def envelope(self) -> EventEnvelope:
        if self.data is None:
            raise ValueError("this frame carries no data")
        return EventEnvelope.model_validate_json(self.data)


async def decode_sse(lines: AsyncIterable[str]) -> AsyncIterator[SseFrame]:
    """Frames from the lines of a `text/event-stream` body (terminators stripped)."""
    frame_id: str | None = None
    data: list[str] = []
    comments: list[str] = []
    async for line in lines:
        if not line:
            if frame_id is not None or data or comments:
                yield SseFrame(
                    id=frame_id,
                    data="\n".join(data) if data else None,
                    comment="\n".join(comments) if comments else None,
                )
            frame_id, data, comments = None, [], []
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if not name:
            comments.append(value)
        elif name == "data":
            data.append(value)
        elif name == "id" and "\0" not in value:
            frame_id = value
