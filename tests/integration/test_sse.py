"""SSE delivery: the LISTEN bus, gap-free resume, scopes and token streams (§8).

P0 T5 acceptance (c)-(g); (a) and (b) are in `test_events_ordering.py`.

Streams run in-process through `StreamingASGITransport` against a real `PgEventBus` on
the test's database, so NOTIFY, LISTEN and the catch-up reads are all real. The bus
polls only every 30 s here, so a test that passes within its few-second timeouts was
served by a notification or a reconnect, never by the poll. Acting identities come from
an `x-test-actor` header in place of the session cookie (`current_actor` is overridden).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from harmony.api.app import create_app
from harmony.api.routes.events import get_sse_options
from harmony.auth.session import Actor, current_actor
from harmony.config import Settings
from harmony.db import repo
from harmony.db.models import (
    Event,
    Member,
    NetworkRequest,
    Org,
    Thread,
    ThreadAccess,
    VisitorSession,
    World,
    WorldEventSeq,
)
from harmony.db.models.choices import MemberRole
from harmony.db.uow import unit_of_work
from harmony.events.bus import (
    SESSION_CHANNEL,
    EventsMayExist,
    SessionPoked,
    StreamMessage,
    StreamPublisher,
    StreamReceived,
    SubscriberHub,
    SubscriptionClosed,
)
from harmony.events.cursor import current_cursor
from harmony.events.pg_bus import PgEventBus, parse_events_payload
from harmony.events.schemas import (
    DemoNotice,
    DurableEvent,
    EntitySnapshot,
    EventEnvelope,
    EventScope,
    HelloTick,
    SessionMemberChanged,
    StreamDelta,
    StreamReset,
    StreamTyping,
    WorldRetired,
)
from harmony.events.sse import SseFrame, SseOptions, Viewer, decode_sse, may_receive, stream_view
from harmony.events.stream import MAX_DELTA_CHARS, PgStreamPublisher, TurnStream
from tests.conftest import ClonedDatabase, settings_without_dotenv
from tests.integration._sse_client import SseReader, StreamingASGITransport, open_stream
from tests.integration._worlds import create_world

# Heartbeats off (60 s) unless a test asks; one typing frame per turn.
OPTIONS = SseOptions(heartbeat_s=60, typing_interval_s=60)
LENS = {"lens": "network"}


def tick(label: str) -> HelloTick:
    return HelloTick(workflow_id=label, n=1, of=1)


def label(envelope: EventEnvelope) -> str:
    event = envelope.event
    return event.workflow_id if isinstance(event, HelloTick) else event.type


def is_label(name: str) -> Callable[[EventEnvelope], bool]:
    return lambda envelope: label(envelope) == name


def as_(name: str) -> dict[str, str]:
    return {"x-test-actor": name}


# --- Pure pieces ----------------------------------------------------------------------

ORG_A, ORG_B, THREAD, MEMBER, SESSION = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()


def _viewer(role: MemberRole = "member", *, lens: bool = False, org: UUID = ORG_A) -> Viewer:
    return Viewer(session_id=SESSION, epoch=0, member_id=MEMBER, org_id=org, role=role, lens=lens)


@pytest.mark.parametrize(
    ("scope", "member", "member_with_access", "admin", "lens"),
    [
        (EventScope.world(), True, True, True, True),
        (EventScope.demo(), True, True, True, True),
        (EventScope.org(ORG_A), True, True, True, True),
        (EventScope.org(ORG_B), False, False, False, True),
        (EventScope.thread(ORG_A, THREAD), False, True, True, True),
        (EventScope.thread(ORG_B, THREAD), False, False, False, True),
        (EventScope.member(MEMBER), True, True, True, True),
        (EventScope.member(uuid4()), False, False, False, True),
        (EventScope.session(SESSION), True, True, True, True),
        (EventScope.session(uuid4()), False, False, False, False),
    ],
)
def test_scope_rules(
    scope: EventScope, member: bool, member_with_access: bool, admin: bool, lens: bool
) -> None:
    def no_access(_: UUID) -> bool:
        return False

    def access(thread_id: UUID) -> bool:
        return thread_id == THREAD

    assert may_receive(_viewer(), scope, no_access) is member
    assert may_receive(_viewer(), scope, access) is member_with_access
    assert may_receive(_viewer("admin"), scope, no_access) is admin
    assert may_receive(_viewer(lens=True), scope, no_access) is lens


def test_stream_view_gives_the_counterparty_typing_only() -> None:
    turn = uuid4()
    delta = StreamDelta(org_id=ORG_A, thread_id=THREAD, turn_id=turn, attempt=1, text="hi")
    reset = StreamReset(org_id=ORG_A, thread_id=THREAD, turn_id=turn, attempt=2)
    typing = StreamTyping(org_id=ORG_A, thread_id=THREAD, turn_id=turn)
    producer, counterparty = _viewer(org=ORG_A), _viewer(org=ORG_B)

    for event in (delta, reset, typing):
        assert stream_view(event, producer, thread_visible=True) == event
        assert stream_view(event, _viewer(lens=True, org=ORG_B), thread_visible=False) == event
        assert stream_view(event, producer, thread_visible=False) is None
        assert stream_view(event, counterparty, thread_visible=False) is None
    assert stream_view(delta, counterparty, thread_visible=True) == typing
    assert stream_view(typing, counterparty, thread_visible=True) == typing
    assert stream_view(reset, counterparty, thread_visible=True) is None


def _message(text: str = "x") -> StreamMessage:
    return StreamMessage(
        world_id=ORG_A,
        event=StreamDelta(org_id=ORG_A, thread_id=THREAD, turn_id=THREAD, attempt=1, text=text),
    )


async def test_hub_coalesces_read_hints_behind_earlier_stream_messages() -> None:
    hub = SubscriberHub(poll_interval_s=60)
    world, session = uuid4(), uuid4()
    async with hub.subscribe(world, session) as subscription:
        hub.events_committed(world, 2)
        hub.stream_received(_message().model_copy(update={"world_id": world}))
        hub.session_poked(session)
        hub.events_committed(world, 5)
        hub.session_poked(session)
        hub.events_committed(uuid4(), 9)  # another world: not ours

        first = await subscription.get()
        assert isinstance(first, StreamReceived)
        assert await subscription.get() == EventsMayExist(world, "notify", 5)
        assert await subscription.get() == SessionPoked(session)

        hub.events_committed(world, 6)
        hub.wake_all()
        assert await subscription.get() == EventsMayExist(world, "reconnect", 6)
        assert hub.subscriber_count == 1
    assert hub.subscriber_count == 0


async def test_hub_polls_when_nothing_happens() -> None:
    hub = SubscriberHub(poll_interval_s=0.05)
    world = uuid4()
    async with hub.subscribe(world, uuid4()) as subscription:
        wake = await asyncio.wait_for(subscription.get(), timeout=1)
    assert wake == EventsMayExist(world, "poll")


async def test_hub_drops_the_oldest_stream_messages_when_a_reader_falls_behind() -> None:
    hub = SubscriberHub(poll_interval_s=60, max_stream_backlog=2)
    world = uuid4()
    async with hub.subscribe(world, uuid4()) as subscription:
        for text_ in ("1", "2", "3"):
            hub.stream_received(_message(text_).model_copy(update={"world_id": world}))
        texts: list[str] = []
        for _ in range(2):
            wake = await subscription.get()
            assert isinstance(wake, StreamReceived)
            assert isinstance(wake.message.event, StreamDelta)
            texts.append(wake.message.event.text)
    assert texts == ["2", "3"]


async def test_closing_the_hub_ends_its_subscriptions() -> None:
    hub = SubscriberHub(poll_interval_s=60)
    async with hub.subscribe(uuid4(), uuid4()) as subscription:
        waiting = asyncio.create_task(subscription.get())
        await asyncio.sleep(0)
        hub.close()
        with pytest.raises(SubscriptionClosed):
            await waiting
    with pytest.raises(SubscriptionClosed):
        async with hub.subscribe(uuid4(), uuid4()):
            pass


def test_events_channel_payloads() -> None:
    world = uuid4()
    assert parse_events_payload(f"{world}:42") == (world, 42)
    assert parse_events_payload("nonsense") is None
    assert parse_events_payload(f"{world}:x") is None


async def test_decode_sse_follows_the_whatwg_framing() -> None:
    async def lines() -> AsyncIterator[str]:
        for line in [": ping", "", "id: 7", "data: {", "data: }", "", "id: 9", "", "data:x", ""]:
            yield line

    frames = [frame async for frame in decode_sse(lines())]
    assert frames == [
        SseFrame(comment="ping"),
        SseFrame(id="7", data="{\n}"),
        SseFrame(id="9"),
        SseFrame(data="x"),
    ]


class _RecordingPublisher(StreamPublisher):
    def __init__(self) -> None:
        self.messages: list[StreamMessage] = []

    async def publish(self, message: StreamMessage) -> None:
        self.messages.append(message)

    async def aclose(self) -> None:
        pass


async def test_turn_stream_coalesces_announces_and_splits() -> None:
    publisher = _RecordingPublisher()
    world_id, turn_id = uuid4(), uuid4()

    def turn(publisher: StreamPublisher, attempt: int, *, enabled: bool = True) -> TurnStream:
        return TurnStream(
            publisher,
            world_id=world_id,
            org_id=ORG_A,
            thread_id=THREAD,
            turn_id=turn_id,
            attempt=attempt,
            enabled=enabled,
            interval_s=60,
        )

    async with turn(publisher, 2) as out:
        for piece in ("Hel", "lo", " ", "x" * MAX_DELTA_CHARS):
            out.push(piece)
    events = [message.event for message in publisher.messages]
    assert [event.type for event in events] == [
        "stream.reset",
        "stream.typing",
        "stream.delta",
        "stream.delta",
    ]
    texts = [event.text for event in events if isinstance(event, StreamDelta)]
    assert "".join(texts) == "Hello " + "x" * MAX_DELTA_CHARS
    assert all(len(text_) <= MAX_DELTA_CHARS for text_ in texts)

    quiet = _RecordingPublisher()
    async with turn(quiet, 1, enabled=False) as out:
        out.push("unwatched")
    assert quiet.messages == []


def test_openapi_documents_the_event_stream() -> None:
    schema = create_app(settings_without_dotenv(app_env="ci")).openapi()
    operation = schema["paths"]["/api/events"]["get"]
    content = operation["responses"]["200"]["content"]
    assert content["text/event-stream"]["schema"] == {"$ref": "#/components/schemas/EventEnvelope"}
    assert "EventEnvelope" in schema["components"]["schemas"]
    parameters = {parameter["name"] for parameter in operation["parameters"]}
    assert {"after", "lens", "last-event-id"} <= parameters


# --- A small world ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cast:
    """Henderson requested thread T from Northlake. Alex and Raj are routed to it on
    their sides, Helen is Northlake's admin, Maya is a Henderson member with no access
    (here, unlike the demo, where she is its admin), and Dana is Ridgeline's admin, a
    bystander. `<name>@network` is the same session resolved in the network world."""

    world_id: UUID
    henderson: UUID
    northlake: UUID
    ridgeline: UUID
    thread_id: UUID
    actors: dict[str, Actor]

    def member(self, name: str) -> UUID:
        return self.actors[name].member_id

    def session(self, name: str) -> UUID:
        return self.actors[name].session_id


async def _build_cast(sessionmaker: async_sessionmaker[AsyncSession]) -> Cast:
    world_id = await create_world(sessionmaker)
    orgs = {slug: uuid4() for slug in ("henderson", "northlake", "ridgeline")}
    people: dict[str, tuple[str, MemberRole]] = {
        "alex": ("henderson", "member"),
        "maya": ("henderson", "member"),
        "helen": ("northlake", "admin"),
        "raj": ("northlake", "member"),
        "dana": ("ridgeline", "admin"),
    }
    actors = {
        name: Actor(
            session_id=uuid4(),
            epoch=0,
            lens="desk",
            world_id=world_id,
            member_id=uuid4(),
            org_id=orgs[org],
            role=role,
        )
        for name, (org, role) in people.items()
    }
    request_id, thread_id = uuid4(), uuid4()
    async with unit_of_work(sessionmaker, world_id) as uow:
        uow.session.add_all(
            Org(world_id=world_id, id=org_id, slug=slug, name=slug.title(), kind="lab")
            for slug, org_id in orgs.items()
        )
        for name, actor in actors.items():
            uow.session.add_all(
                [
                    Member(
                        world_id=world_id,
                        id=actor.member_id,
                        org_id=actor.org_id,
                        slug=name,
                        name=name.title(),
                        role=actor.role,
                    ),
                    VisitorSession(
                        id=actor.session_id,
                        desk_world_id=world_id,
                        desk_member_id=actor.member_id,
                        epoch=0,
                        budget_lineage=uuid4(),
                    ),
                ]
            )
        uow.session.add_all(
            [
                NetworkRequest(
                    world_id=world_id,
                    id=request_id,
                    requester_org_id=orgs["henderson"],
                    brief="XRD on 12 samples",
                ),
                Thread(
                    world_id=world_id,
                    id=thread_id,
                    request_id=request_id,
                    requester_org_id=orgs["henderson"],
                    responder_org_id=orgs["northlake"],
                ),
                *(
                    ThreadAccess(
                        world_id=world_id,
                        thread_id=thread_id,
                        member_id=actors[name].member_id,
                        reason="routed",
                    )
                    for name in ("alex", "raj")
                ),
            ]
        )
        await uow.commit()
    in_network = {f"{name}@network": replace(a, lens="network") for name, a in actors.items()}
    return Cast(
        world_id=world_id,
        henderson=orgs["henderson"],
        northlake=orgs["northlake"],
        ridgeline=orgs["ridgeline"],
        thread_id=thread_id,
        actors=actors | in_network,
    )


@pytest.fixture
async def cast(db_sessionmaker: async_sessionmaker[AsyncSession]) -> Cast:
    return await _build_cast(db_sessionmaker)


@pytest.fixture
async def bus(database: ClonedDatabase) -> AsyncIterator[PgEventBus]:
    bus = PgEventBus(database.dsn, poll_interval_s=30, reconnect_delays_s=(1.0,))
    await bus.start()
    try:
        yield bus
    finally:
        await bus.aclose()


def _actor_from_header(actors: Mapping[str, Actor]) -> Callable[[Request], Actor]:
    def actor(request: Request) -> Actor:
        try:
            return actors[request.headers["x-test-actor"]]
        except KeyError:
            raise HTTPException(401) from None

    return actor


def _app(
    settings: Settings, engine: AsyncEngine, bus: PgEventBus | None, actors: Mapping[str, Actor]
) -> FastAPI:
    app = create_app(settings, engine=engine, event_bus=bus)
    app.dependency_overrides[current_actor] = _actor_from_header(actors)
    app.dependency_overrides[get_sse_options] = lambda: OPTIONS
    return app


@pytest.fixture
def app(app_settings: Settings, db_engine: AsyncEngine, bus: PgEventBus, cast: Cast) -> FastAPI:
    return _app(app_settings, db_engine, bus, cast.actors)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = StreamingASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def emit(
    sessionmaker: async_sessionmaker[AsyncSession],
    world_id: UUID,
    *events: tuple[DurableEvent, EventScope],
) -> list[int]:
    async with unit_of_work(sessionmaker, world_id) as uow:
        for event, scope in events:
            uow.add_event(event, scope)
        return list(await uow.commit())


async def emit_ticks(
    sessionmaker: async_sessionmaker[AsyncSession], world_id: UUID, *labels: str
) -> list[int]:
    return await emit(
        sessionmaker, world_id, *((tick(name), EventScope.world()) for name in labels)
    )


async def snapshot_cursor(sessionmaker: async_sessionmaker[AsyncSession], world_id: UUID) -> int:
    async with sessionmaker() as session:
        return await current_cursor(session, world_id)


# --- Resume, cursors and the LISTEN connection -------------------------------------------


async def test_snapshot_cursor_with_after_delivers_events_committed_before_subscribing(
    client: httpx.AsyncClient, cast: Cast, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Acceptance (d)."""
    await emit_ticks(db_sessionmaker, cast.world_id, "before")
    cursor = await snapshot_cursor(db_sessionmaker, cast.world_id)
    between = await emit_ticks(db_sessionmaker, cast.world_id, "between-1", "between-2")

    async with open_stream(client, headers=as_("raj"), params={"after": cursor}) as reader:
        received = await reader.envelopes_until(is_label("between-2"))
        assert [(label(e), e.seq) for e in received] == list(
            zip(["between-1", "between-2"], between, strict=True)
        )
        later = await emit_ticks(db_sessionmaker, cast.world_id, "later")
        envelope = await reader.next_envelope()
        assert (label(envelope), envelope.seq) == ("later", later[0])


async def test_reconnecting_with_last_event_id_resumes_without_gaps(
    client: httpx.AsyncClient, cast: Cast, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    start = await snapshot_cursor(db_sessionmaker, cast.world_id)
    async with open_stream(client, headers=as_("alex"), params={"after": start}) as reader:
        await emit_ticks(db_sessionmaker, cast.world_id, "a", "b")
        await emit_ticks(db_sessionmaker, cast.world_id, "c")
        first = await reader.envelopes_until(is_label("c"))
        last_event_id = reader.last_event_id
    assert last_event_id is not None
    assert last_event_id == str(first[-1].seq)

    await emit_ticks(db_sessionmaker, cast.world_id, "d", "e")  # while disconnected
    # As EventSource does: the original URL, plus Last-Event-ID, which wins over ?after=.
    headers = as_("alex") | {"Last-Event-ID": last_event_id}
    async with open_stream(client, headers=headers, params={"after": start}) as reader:
        await emit_ticks(db_sessionmaker, cast.world_id, "f")
        second = await reader.envelopes_until(is_label("f"))

    received = first + second
    assert [label(e) for e in received] == ["a", "b", "c", "d", "e", "f"]
    assert [e.seq for e in received] == list(range(start + 1, start + 7))


async def test_filtered_events_still_move_last_event_id(
    client: httpx.AsyncClient, cast: Cast, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with open_stream(client, headers=as_("alex")) as reader:
        hidden = await emit(
            db_sessionmaker, cast.world_id, (tick("northlake-only"), EventScope.org(cast.northlake))
        )
        frame = await reader.next_frame()
        assert frame == SseFrame(id=str(hidden[0]))  # no data: fires no event in a browser
    async with open_stream(
        client, headers=as_("alex") | {"Last-Event-ID": str(hidden[0])}
    ) as reader:
        visible = await emit_ticks(db_sessionmaker, cast.world_id, "next")
        envelope = await reader.next_envelope()
        assert (label(envelope), envelope.seq) == ("next", visible[0])


async def test_subscribers_catch_up_within_6s_after_the_listen_connection_is_killed(
    client: httpx.AsyncClient,
    cast: Cast,
    db_engine: AsyncEngine,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    bus: PgEventBus,
) -> None:
    """Acceptance (c). The bus polls every 30 s here, so only its reconnect can help."""
    async with open_stream(client, headers=as_("raj")) as reader:
        await emit_ticks(db_sessionmaker, cast.world_id, "before")
        assert label(await reader.next_envelope()) == "before"

        old_pid = bus.listener_pid
        assert old_pid is not None
        async with db_engine.connect() as conn:
            killed = await conn.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": old_pid})
        assert killed
        await _until(lambda: bus.listener_pid is None)

        # Committed while nobody is listening: its NOTIFY is lost for good.
        await emit_ticks(db_sessionmaker, cast.world_id, "while-down")
        envelope = await reader.next_envelope(wait_s=6)
        assert label(envelope) == "while-down"
        assert bus.listener_pid not in (None, old_pid)

        # Notifications flow again on the new connection.
        await emit_ticks(db_sessionmaker, cast.world_id, "after")
        assert label(await reader.next_envelope(wait_s=2)) == "after"


async def _until(condition: Callable[[], bool], *, attempts: int = 500) -> None:
    for _ in range(attempts):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


async def test_heartbeats_are_comments(app: FastAPI, client: httpx.AsyncClient, cast: Cast) -> None:
    app.dependency_overrides[get_sse_options] = lambda: SseOptions(heartbeat_s=0.1)
    async with open_stream(client, headers=as_("maya")) as reader:
        frame = await reader.next_frame(wait_s=2)
    assert frame is not None
    assert frame.comment is not None
    assert frame.data is None


async def test_bad_cursors_are_refused(
    client: httpx.AsyncClient, cast: Cast, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    latest = await snapshot_cursor(db_sessionmaker, cast.world_id)
    ahead = await client.get("/api/events", headers=as_("raj"), params={"after": latest + 1})
    assert ahead.status_code == 409
    garbage = await client.get("/api/events", headers=as_("raj") | {"Last-Event-ID": "x"})
    assert garbage.status_code == 400
    negative = await client.get("/api/events", headers=as_("raj"), params={"after": -1})
    assert negative.status_code == 422


async def test_a_stream_for_another_epoch_is_refused(client: httpx.AsyncClient, cast: Cast) -> None:
    """After a reset or the other world's act-as, the browser reconnects to the same URL
    by itself; naming the snapshot's epoch makes that a 409 instead of a live stream."""
    current = cast.actors["raj"].epoch
    stale = await client.get("/api/events", headers=as_("raj"), params={"epoch": current + 1})
    assert stale.status_code == 409
    async with open_stream(client, headers=as_("raj"), params={"epoch": current}):
        pass  # the current epoch opens as usual


async def test_unknown_world_404s_and_a_missing_bus_503s(
    app_settings: Settings, db_engine: AsyncEngine, bus: PgEventBus, cast: Cast
) -> None:
    ghost = replace(cast.actors["raj"], world_id=uuid4())
    actors = cast.actors | {"ghost": ghost}
    for event_bus, name, expected in ((bus, "ghost", 404), (None, "raj", 503)):
        transport = StreamingASGITransport(_app(app_settings, db_engine, event_bus, actors))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/events", headers=as_(name))
        assert response.status_code == expected


# --- Scopes ----------------------------------------------------------------------------------


async def test_each_viewer_receives_only_their_scopes(
    client: httpx.AsyncClient, cast: Cast, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Acceptance (e), durable half: no viewer sees another org's org-scoped events."""
    nl, hn, thread = cast.northlake, cast.henderson, cast.thread_id
    events: list[tuple[DurableEvent, EventScope]] = [
        (tick("world"), EventScope.world()),
        (tick("org:northlake"), EventScope.org(nl)),
        (tick("org:henderson"), EventScope.org(hn)),
        (tick("thread:northlake"), EventScope.thread(nl, thread)),
        (tick("thread:henderson"), EventScope.thread(hn, thread)),
        (tick("member:raj"), EventScope.member(cast.member("raj"), nl)),
        (tick("session:alex"), EventScope.session(cast.session("alex"))),
        (tick("session:raj"), EventScope.session(cast.session("raj"))),
        (tick("end"), EventScope.world()),
    ]
    expected = {
        "alex": ["world", "org:henderson", "thread:henderson", "session:alex", "end"],
        "maya": ["world", "org:henderson", "end"],
        "raj": ["world", "org:northlake", "thread:northlake", "member:raj", "session:raj", "end"],
        "helen": ["world", "org:northlake", "thread:northlake", "end"],
        "dana": ["world", "end"],
        # The lens (opened from Alex's session) sees both sides, but no other session's events.
        "lens": [label(e) for e in _envelopes(events) if label(e) != "session:raj"],
        # Asked for in a desk world, it is Alex's own stream.
        "desk lens": ["world", "org:henderson", "thread:henderson", "session:alex", "end"],
    }
    lenses = {"lens": ("alex@network", LENS), "desk lens": ("alex", LENS)}
    async with AsyncExitStack() as stack:
        readers: dict[str, SseReader] = {}
        for name in expected:
            actor, params = lenses.get(name, (name, None))
            readers[name] = await stack.enter_async_context(
                open_stream(client, headers=as_(actor), params=params)
            )
        await emit(db_sessionmaker, cast.world_id, *events)
        for name, reader in readers.items():
            received = await reader.envelopes_until(is_label("end"))
            assert [label(e) for e in received] == expected[name], name


async def test_the_lens_never_receives_what_an_org_tells_its_own_claude(
    client: httpx.AsyncClient, cast: Cast, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """EDIT.md §7: a side's steers and guidance reach its own members, never the
    network view, which sees every other event of that side."""

    def snapshot(entity_type: str) -> tuple[DurableEvent, EventScope]:
        event = EntitySnapshot(entity_type=entity_type, entity_id=uuid4(), version=1, data={})
        return event, EventScope.thread(cast.northlake, cast.thread_id)

    def shown(envelope: EventEnvelope) -> str:
        event = envelope.event
        return event.entity_type if isinstance(event, EntitySnapshot) else label(envelope)

    events = [snapshot("steer"), snapshot("rep_guidance"), snapshot("internal_note")]
    expected = {
        "raj": ["steer", "rep_guidance", "internal_note", "end"],
        "alex@network": ["internal_note", "end"],
    }
    async with AsyncExitStack() as stack:
        readers = {
            name: await stack.enter_async_context(
                open_stream(client, headers=as_(name), params=LENS if "@" in name else None)
            )
            for name in expected
        }
        await emit(db_sessionmaker, cast.world_id, *events, (tick("end"), EventScope.world()))
        for name, reader in readers.items():
            received = await reader.envelopes_until(is_label("end"))
            assert [shown(e) for e in received] == expected[name], name


async def test_a_thread_event_is_delivered_even_if_its_cached_access_expires_mid_batch(
    app_settings: Settings,
    db_engine: AsyncEngine,
    bus: PgEventBus,
    cast: Cast,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Access is resolved once per batch; an answer that expires while the batch is
    being delivered (here: at once) must not drop an event the viewer may see."""
    app = _app(app_settings, db_engine, bus, cast.actors)
    app.dependency_overrides[get_sse_options] = lambda: replace(OPTIONS, access_ttl_s=0)
    transport = StreamingASGITransport(app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        open_stream(client, headers=as_("raj")) as raj,
    ):
        await emit(
            db_sessionmaker,
            cast.world_id,
            (tick("thread:northlake"), EventScope.thread(cast.northlake, cast.thread_id)),
            (tick("thread:henderson"), EventScope.thread(cast.henderson, cast.thread_id)),
            (tick("end"), EventScope.world()),
        )
        received = await raj.envelopes_until(is_label("end"))
    assert [label(e) for e in received] == ["thread:northlake", "end"]


def _envelopes(events: list[tuple[DurableEvent, EventScope]]) -> list[EventEnvelope]:
    return [
        EventEnvelope(world_id=uuid4(), seq=None, created_at=None, scope=scope, event=event)
        for event, scope in events
    ]


async def test_deltas_reach_only_the_producing_org_and_the_lens(
    client: httpx.AsyncClient,
    cast: Cast,
    database: ClonedDatabase,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Acceptance (e), stream half: the counterparty gets `stream.typing`, never text."""
    viewers = ["raj", "helen", "alex", "maya", "dana", "lens"]
    publisher = PgStreamPublisher(database.dsn)
    turn_id = uuid4()
    async with AsyncExitStack() as stack:
        stack.push_async_callback(publisher.aclose)
        readers: dict[str, SseReader] = {}
        for name in viewers:
            actor, params = ("raj@network", LENS) if name == "lens" else (name, None)
            readers[name] = await stack.enter_async_context(
                open_stream(client, headers=as_(actor), params=params)
            )
        async with TurnStream(
            publisher,
            world_id=cast.world_id,
            org_id=cast.northlake,
            thread_id=cast.thread_id,
            turn_id=turn_id,
            attempt=2,
            interval_s=0.01,
        ) as out:
            out.push("Hello")
            await out.flush()
            out.push(", Henderson")
        await emit(
            db_sessionmaker,
            cast.world_id,
            (tick("committed:northlake"), EventScope.thread(cast.northlake, cast.thread_id)),
            (tick("end"), EventScope.world()),
        )

        received = {
            name: await reader.envelopes_until(is_label("end")) for name, reader in readers.items()
        }

    producer_view = ["stream.reset", "stream.typing", "stream.delta", "committed:northlake", "end"]
    for name in ("raj", "helen", "lens"):
        assert _collapse_deltas(received[name]) == producer_view, name
        text_ = "".join(e.event.text for e in received[name] if isinstance(e.event, StreamDelta))
        assert text_ == "Hello, Henderson"
    assert [label(e) for e in received["alex"]] == ["stream.typing", "end"]
    assert [label(e) for e in received["maya"]] == ["end"]
    assert [label(e) for e in received["dana"]] == ["end"]
    for envelope in received["alex"][:1]:
        assert envelope.seq is None
        assert envelope.scope == EventScope.thread(cast.northlake, cast.thread_id)


def _collapse_deltas(envelopes: list[EventEnvelope]) -> list[str]:
    labels: list[str] = []
    for envelope in envelopes:
        name = label(envelope)
        if not (name == "stream.delta" and labels and labels[-1] == name):
            labels.append(name)
    return labels


async def test_demo_events_carry_no_entity_payload(
    client: httpx.AsyncClient,
    cast: Cast,
    db_engine: AsyncEngine,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Acceptance (f), including a snapshot forced into demo scope behind the
    UnitOfWork's back: the stream drops it rather than show entity data to everyone."""
    entity_id = uuid4()
    async with open_stream(client, headers=as_("dana")) as reader:
        await emit(
            db_sessionmaker,
            cast.world_id,
            (
                DemoNotice(kind="escalation.created", org_slug="northlake", entity_id=entity_id),
                EventScope.demo(),
            ),
        )
        snapshot = EntitySnapshot(
            entity_type="thread", entity_id=cast.thread_id, version=1, data={"secret": "x"}
        )
        await _insert_raw(db_engine, cast.world_id, snapshot, scope_kind="demo")
        await emit_ticks(db_sessionmaker, cast.world_id, "end")

        frames: list[SseFrame] = []
        while not frames or label(frames[-1].envelope()) != "end":
            frame = await reader.next_frame()
            assert frame is not None
            if frame.data is not None:
                frames.append(frame)

    assert [label(frame.envelope()) for frame in frames] == ["demo.notice", "end"]
    demo = json.loads(frames[0].data or "")
    assert demo["scope"]["kind"] == "demo"
    assert demo["event"] == {
        "type": "demo.notice",
        "kind": "escalation.created",
        "org_slug": "northlake",
        "entity_id": str(entity_id),
    }


async def _insert_raw(
    engine: AsyncEngine, world_id: UUID, snapshot: EntitySnapshot, *, scope_kind: str
) -> None:
    async with engine.begin() as conn:
        seq = await conn.scalar(
            update(WorldEventSeq)
            .where(WorldEventSeq.world_id == world_id)
            .values(seq=WorldEventSeq.seq + 1)
            .returning(WorldEventSeq.seq)
        )
        await conn.execute(
            insert(Event).values(
                world_id=world_id,
                seq=seq,
                type=snapshot.type,
                scope_kind=scope_kind,
                entity_type=snapshot.entity_type,
                entity_id=snapshot.entity_id,
                entity_version=snapshot.version,
                payload=snapshot.model_dump(mode="json"),
            )
        )
        await conn.execute(select(func.pg_notify("events", f"{world_id}:{seq}")))


# --- Ending streams --------------------------------------------------------------------------


async def _act_as(
    sessionmaker: async_sessionmaker[AsyncSession],
    cast: Cast,
    session_name: str,
    member_name: str,
    *,
    with_event: bool = True,
) -> None:
    """What act-as does (T4): bump the epoch, emit `session.member_changed`, poke."""
    session_id, member_id = cast.session(session_name), cast.member(member_name)
    async with unit_of_work(sessionmaker, cast.world_id) as uow:
        epoch = await uow.session.scalar(
            update(VisitorSession)
            .where(VisitorSession.id == session_id)
            .values(epoch=VisitorSession.epoch + 1, desk_member_id=member_id)
            .returning(VisitorSession.epoch)
        )
        assert epoch is not None
        if with_event:
            uow.add_event(
                SessionMemberChanged(session_id=session_id, member_id=member_id, epoch=epoch),
                EventScope.session(session_id),
            )
        await uow.session.execute(select(func.pg_notify(SESSION_CHANNEL, str(session_id))))
        await uow.commit()


async def test_an_epoch_bump_closes_the_stream_after_its_member_changed_event(
    client: httpx.AsyncClient, cast: Cast, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Acceptance (g). Other sessions' streams stay open."""
    async with (
        open_stream(client, headers=as_("alex")) as alex,
        open_stream(client, headers=as_("raj")) as raj,
    ):
        await emit_ticks(db_sessionmaker, cast.world_id, "before")
        await _act_as(db_sessionmaker, cast, "alex", "maya")
        frames = await alex.until_end()
        assert [label(f.envelope()) for f in frames if f.data] == [
            "before",
            "session.member_changed",
        ]

        await emit_ticks(db_sessionmaker, cast.world_id, "after")
        assert [label(e) for e in await raj.envelopes_until(is_label("after"))] == [
            "before",
            "after",
        ]


async def test_an_epoch_bump_from_the_other_world_closes_the_stream_too(
    client: httpx.AsyncClient, cast: Cast, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Act-as in the session's other world leaves no event here; the session poke does it."""
    async with open_stream(client, headers=as_("alex")) as alex:
        await _act_as(db_sessionmaker, cast, "alex", "maya", with_event=False)
        frames = await alex.until_end()
    assert [frame for frame in frames if frame.data] == []


async def test_a_stream_opened_under_a_stale_epoch_ends_at_once(
    app: FastAPI, client: httpx.AsyncClient, cast: Cast
) -> None:
    stale = replace(cast.actors["raj"], epoch=7)
    app.dependency_overrides[current_actor] = _actor_from_header({"stale": stale})
    async with open_stream(client, headers=as_("stale")) as reader:
        assert await reader.until_end() == []


async def test_world_retired_is_the_last_event(
    client: httpx.AsyncClient, cast: Cast, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with open_stream(client, headers=as_("helen")) as reader:
        async with unit_of_work(db_sessionmaker, cast.world_id, require_live=False) as uow:
            world = await uow.session.get_one(World, cast.world_id)
            world.status = "retired"
            uow.add_event(WorldRetired(), EventScope.world())
            await uow.commit()
        frames = await reader.until_end()
    assert [label(f.envelope()) for f in frames if f.data] == ["world.retired"]


async def _wipe_silently(sessionmaker: async_sessionmaker[AsyncSession], world_id: UUID) -> None:
    """An inline wipe whose `world.retired` row was deleted before any stream read it:
    the world is `retired` and its rows (events included) are gone."""
    async with sessionmaker() as session, session.begin():
        await session.execute(update(World).where(World.id == world_id).values(status="retired"))
        await repo.delete_world_rows(session, world_id)


async def test_a_session_poke_ends_a_stream_whose_world_was_wiped(
    client: httpx.AsyncClient, cast: Cast, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with open_stream(client, headers=as_("raj")) as raj:
        await emit_ticks(db_sessionmaker, cast.world_id, "before")
        assert label(await raj.next_envelope()) == "before"
        await _wipe_silently(db_sessionmaker, cast.world_id)
        async with db_sessionmaker() as session, session.begin():
            await session.execute(select(func.pg_notify(SESSION_CHANNEL, str(cast.session("raj")))))
        frames = await raj.until_end()
    assert [frame for frame in frames if frame.data] == []


async def test_a_poll_ends_a_stream_whose_world_was_wiped(
    app_settings: Settings,
    db_engine: AsyncEngine,
    database: ClonedDatabase,
    cast: Cast,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Nothing pokes the other viewers of a wiped world; the bus's poll ends their streams."""
    polling = PgEventBus(database.dsn, poll_interval_s=0.2, reconnect_delays_s=(1.0,))
    await polling.start()
    try:
        transport = StreamingASGITransport(_app(app_settings, db_engine, polling, cast.actors))
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as client,
            open_stream(client, headers=as_("helen")) as helen,
        ):
            await _wipe_silently(db_sessionmaker, cast.world_id)
            frames = await helen.until_end()
        assert [frame for frame in frames if frame.data] == []
    finally:
        await polling.aclose()
