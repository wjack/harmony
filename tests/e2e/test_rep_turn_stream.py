"""A representative turn, end to end on the real streaming path (`AgentActivities.rep_turn`).

Northlake's representative answers Henderson's opening on a Henderson -> Northlake
thread. Everything is real except the model and the activity's caller: the API in-process
with real cookie sessions, the SSE route over a real `PgEventBus`, Postgres (this test's
database) and the worker's stream publisher. The model is `ScriptedModel` over
tests/fixtures/cassettes, paired with `FreeSpend`, exactly as `harmony worker` builds it
in scripted mode (`model_runtime`); the activity runs in an `ActivityEnvironment`, one
attempt at a time, as `ThreadWorkflow` would run and retry it.

- Helen (Northlake admin) receives `stream.delta` text, then the committed message;
  Alex (Henderson, routed) receives only `stream.typing`, then the message; Raj
  (Northlake, made a member here, no access) receives nothing about the thread.
- An attempt that crashes after its message is committed is retried without a second
  model call for that step and without a duplicate message.
- A model that is silent for 12 s (as Opus thinking is) still shows the turn typing:
  `stream.typing` repeats every 5 s until the turn ends (3.5).
- No spend-ledger line is written.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from itertools import takewhile
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.testing import ActivityEnvironment

from harmony.agents.runtime import MessageSent
from harmony.api.app import create_app
from harmony.auth.session import SESSION_COOKIE, sign_session_id
from harmony.config import Settings
from harmony.db.models import (
    Event,
    ModelCall,
    NetworkRequest,
    Thread,
    ThreadAccess,
    ThreadMessage,
    VisitorSession,
)
from harmony.db.uow import unit_of_work
from harmony.domain.threads import THREAD_MESSAGE_ENTITY
from harmony.events.pg_bus import PgEventBus
from harmony.events.schemas import (
    EntitySnapshot,
    EventEnvelope,
    EventScope,
    HelloTick,
    StreamDelta,
    StreamReset,
    StreamTyping,
)
from harmony.llm import HeartbeatCallback, ModelRequest, ModelResult, StreamEventCallback
from harmony.llm.budget import FreeSpend
from harmony.llm.scripted import ScriptedModel
from harmony.temporal.activities.agents import AgentActivities, RepTurnInput, RepTurnResult
from harmony.temporal.worker import ModelRuntime, model_runtime
from tests.conftest import ClonedDatabase
from tests.integration._sse_client import SseReader, StreamingASGITransport, open_stream
from tests.integration._visitors import demo_id, visitor
from tests.integration._worlds import demote_to_member

THREAD_KEY = "rep-turn"  # the cassette's thread (tests/fixtures/cassettes/rep_turn.yaml)
OPENING = (
    "Hello, this is Claude for Henderson Frame Lab. We need CT scans and compression tests on "
    "12 titanium lattice coupons by Nov 6. Can you help?"
)
HENDERSON, NORTHLAKE = demo_id("org", "henderson"), demo_id("org", "northlake")
WAIT_S = 30.0

type Sessionmaker = async_sessionmaker[AsyncSession]


@dataclass(frozen=True, slots=True)
class Stage:
    """One world with an open Henderson -> Northlake thread, and its viewers' cookies."""

    world_id: UUID
    thread_id: UUID
    turn_id: UUID
    cookies: dict[str, str]  # viewer -> signed session cookie


async def _stage(app: FastAPI, settings: Settings, sm: Sessionmaker) -> Stage:
    async with visitor(app) as first:
        world_id = UUID((await first.start())["world_id"])
    # Everyone in the demo is an admin; as a member, Raj sees only what he's routed.
    await demote_to_member(sm, world_id, "raj")
    thread_id, request_id = uuid4(), uuid4()
    async with unit_of_work(sm, world_id) as uow:
        session = uow.session
        session.add(
            NetworkRequest(
                world_id=world_id,
                id=request_id,
                requester_org_id=HENDERSON,
                brief="CT and compression tests on 12 lattice coupons by Nov 6",
                created_by_member_id=demo_id("member", "alex"),
            )
        )
        session.add(
            Thread(
                world_id=world_id,
                id=thread_id,
                request_id=request_id,
                requester_org_id=HENDERSON,
                responder_org_id=NORTHLAKE,
                state="active",
                side_to_move="responder",
            )
        )
        session.add(
            ThreadMessage(
                world_id=world_id,
                id=uuid4(),
                thread_id=thread_id,
                seq=1,
                from_org_id=HENDERSON,
                kind="message",
                body=OPENING,
                provenance="agent",
            )
        )
        # Alex is routed on Henderson's side. Helen sees Northlake's threads as its
        # admin; Raj (Northlake) has no access, nor has Maya (Henderson).
        session.add(
            ThreadAccess(
                world_id=world_id,
                thread_id=thread_id,
                member_id=demo_id("member", "alex"),
                reason="routed",
            )
        )
        # One visitor session per viewer, all in this world (a real visitor acts as
        # one member at a time; the test watches several at once).
        sessions = {name: uuid4() for name in ("helen", "raj", "alex")}
        for name, session_id in sessions.items():
            session.add(
                VisitorSession(
                    id=session_id,
                    desk_world_id=world_id,
                    desk_member_id=demo_id("member", name),
                )
            )
        await uow.commit()
    secret = settings.session_secret_value()
    cookies = {name: sign_session_id(session_id, secret) for name, session_id in sessions.items()}
    return Stage(world_id, thread_id, uuid4(), cookies)


@asynccontextmanager
async def _streams(
    app: FastAPI, stage: Stage, viewers: dict[str, tuple[str, dict[str, str]]]
) -> AsyncIterator[dict[str, SseReader]]:
    """Open `/api/events` for each viewer: name -> (whose session, extra params)."""
    async with AsyncExitStack() as stack:
        readers: dict[str, SseReader] = {}
        for name, (member, params) in viewers.items():
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=StreamingASGITransport(app),
                    base_url="https://harmony.test",
                    cookies={SESSION_COOKIE: stage.cookies[member]},
                )
            )
            readers[name] = await stack.enter_async_context(open_stream(client, params=params))
        yield readers


VIEWERS = {
    "helen": ("helen", {}),
    "alex": ("alex", {}),
    "raj": ("raj", {}),
}


def _is_message(envelope: EventEnvelope) -> bool:
    event = envelope.event
    return isinstance(event, EntitySnapshot) and event.entity_type == THREAD_MESSAGE_ENTITY


def _is_marker(label: str) -> Callable[[EventEnvelope], bool]:
    return lambda e: isinstance(e.event, HelloTick) and e.event.workflow_id == label


def _stream_events(envelopes: Sequence[EventEnvelope]) -> list[Any]:
    return [e.event for e in envelopes if e.seq is None]


async def _mark(sm: Sessionmaker, world_id: UUID, label: str) -> None:
    """A world-scoped marker: every viewer reads up to it, so 'nothing else' is checkable."""
    async with unit_of_work(sm, world_id) as uow:
        uow.add_event(HelloTick(workflow_id=label, n=1, of=1), EventScope.world())
        await uow.commit()


def _turn(stage: Stage) -> RepTurnInput:
    return RepTurnInput(
        world_id=stage.world_id,
        thread_id=stage.thread_id,
        side="responder",
        org_id=NORTHLAKE,
        cassette_thread=THREAD_KEY,
        turn=1,
        turn_id=stage.turn_id,
    )


async def _run_turn(agents: AgentActivities, stage: Stage, *, attempt: int = 1) -> RepTurnResult:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, attempt=attempt)
    return await env.run(agents.rep_turn, _turn(stage))


def _agents(
    runtime: ModelRuntime, settings: Settings, sm: Sessionmaker, model: Any
) -> AgentActivities:
    return AgentActivities(
        sm, model, runtime.guard, runtime.publisher, model_override=settings.model_override
    )


def _sent(result: RepTurnResult) -> UUID:
    (sent,) = [effect for effect in result.effects if isinstance(effect, MessageSent)]
    return sent.message_id


async def _count(sm: Sessionmaker, statement: Any) -> int:
    async with sm() as session:
        return int(await session.scalar(statement) or 0)


async def _turn_rows(sm: Sessionmaker, stage: Stage) -> tuple[int, int, int]:
    """(Northlake messages in the thread, model calls, thread_message events) in the world."""
    messages = await _count(
        sm,
        select(func.count())
        .select_from(ThreadMessage)
        .where(
            ThreadMessage.world_id == stage.world_id,
            ThreadMessage.thread_id == stage.thread_id,
            ThreadMessage.from_org_id == NORTHLAKE,
        ),
    )
    calls = await _count(
        sm, select(func.count()).select_from(ModelCall).where(ModelCall.world_id == stage.world_id)
    )
    message_events = await _count(
        sm,
        select(func.count())
        .select_from(Event)
        .where(Event.world_id == stage.world_id, Event.entity_type == THREAD_MESSAGE_ENTITY),
    )
    return messages, calls, message_events


def _assert_no_ledger(settings: Settings) -> None:
    ledger = settings.spend_ledger_path
    assert ledger is not None
    assert not ledger.exists() or ledger.read_text() == "", "a scripted turn wrote the ledger"


@pytest.fixture
async def bus(database: ClonedDatabase) -> AsyncIterator[PgEventBus]:
    bus = PgEventBus(database.dsn, poll_interval_s=30)
    await bus.start()
    try:
        yield bus
    finally:
        await bus.aclose()


@pytest.fixture
def app(app_settings: Settings, db_engine: AsyncEngine, bus: PgEventBus) -> FastAPI:
    return create_app(app_settings, engine=db_engine, event_bus=bus)


@pytest.fixture
async def runtime(
    app_settings: Settings, db_sessionmaker: Sessionmaker
) -> AsyncIterator[ModelRuntime]:
    runtime = model_runtime(app_settings, db_sessionmaker)
    try:
        yield runtime
    finally:
        await runtime.publisher.aclose()


async def test_the_turn_streams_to_the_producing_org_and_types_to_the_counterparty(
    app: FastAPI,
    app_settings: Settings,
    db_sessionmaker: Sessionmaker,
    runtime: ModelRuntime,
) -> None:
    model = runtime.client
    assert isinstance(model, ScriptedModel)
    assert isinstance(runtime.guard, FreeSpend)
    stage = await _stage(app, app_settings, db_sessionmaker)
    agents = _agents(runtime, app_settings, db_sessionmaker, model)

    async with _streams(app, stage, VIEWERS) as streams:
        result = await _run_turn(agents, stage)
        received = {
            name: await streams[name].envelopes_until(_is_message, WAIT_S)
            for name in ("helen", "alex")
        }
        await _mark(db_sessionmaker, stage.world_id, "after-turn")
        raj = await streams["raj"].envelopes_until(_is_marker("after-turn"), WAIT_S)

    assert result.stop == "end_turn"
    body = received["helen"][-1].event
    assert isinstance(body, EntitySnapshot)
    text = body.data["body"]
    assert isinstance(text, str)
    assert text.startswith("Thanks for reaching out.")

    events = _stream_events(received["helen"])
    deltas = [event.text for event in events if isinstance(event, StreamDelta)]
    assert deltas, "Helen received no stream.delta"
    assert "".join(deltas) == text, "Helen's deltas are not the message body"
    assert not any(isinstance(event, StreamReset) for event in events)

    alex = _stream_events(received["alex"])
    assert alex, "Alex saw no typing indicator"
    assert all(isinstance(event, StreamTyping) for event in alex), alex
    alex_message = received["alex"][-1].event
    assert isinstance(alex_message, EntitySnapshot)
    assert alex_message.data["body"] == text
    assert alex_message.entity_id == _sent(result)

    assert not _stream_events(raj), "Raj (no access) received stream events"
    assert not any(isinstance(e.event, EntitySnapshot) for e in raj), "Raj saw thread data"

    assert model.call_count == 2
    assert await _turn_rows(db_sessionmaker, stage) == (1, 2, 2)
    _assert_no_ledger(app_settings)


class _CrashOnce(ScriptedModel):
    """Dies on the turn's second step in its first attempt, after the first step's
    response was stored and its message committed, as a worker dying there would."""

    async def call(
        self,
        request: ModelRequest,
        *,
        on_stream_event: StreamEventCallback | None = None,
        heartbeat: HeartbeatCallback | None = None,
    ) -> ModelResult:
        if request.key.step_idx == 1 and request.key.attempt == 1:
            raise RuntimeError("injected crash")
        return await super().call(request, on_stream_event=on_stream_event, heartbeat=heartbeat)


async def test_a_crashed_attempt_is_retried_without_a_second_call_or_message(
    app: FastAPI,
    app_settings: Settings,
    db_sessionmaker: Sessionmaker,
    runtime: ModelRuntime,
) -> None:
    model = _CrashOnce.from_paths(app_settings.cassette_dir)
    stage = await _stage(app, app_settings, db_sessionmaker)
    agents = _agents(runtime, app_settings, db_sessionmaker, model)

    async with _streams(app, stage, {"helen": VIEWERS["helen"]}) as streams:
        with pytest.raises(RuntimeError, match="injected crash"):
            await _run_turn(agents, stage, attempt=1)
        result = await _run_turn(agents, stage, attempt=2)
        await _mark(db_sessionmaker, stage.world_id, "after-turn")
        helen = await streams["helen"].envelopes_until(_is_marker("after-turn"), WAIT_S)

    # The retry replayed the stored first step and found its committed message.
    assert result.stop == "end_turn"
    assert model.call_count == 2
    assert await _turn_rows(db_sessionmaker, stage) == (1, 2, 2)
    messages = [e for e in helen if _is_message(e)]
    assert len(messages) == 1, "the message was delivered more than once"
    assert isinstance(messages[0].event, EntitySnapshot)
    assert messages[0].event.entity_id == _sent(result)
    # The retry announced itself, so clients drop the first attempt's text.
    resets = [e for e in _stream_events(helen) if isinstance(e, StreamReset)]
    assert [reset.attempt for reset in resets] == [2]
    _assert_no_ledger(app_settings)


class _Thinks(ScriptedModel):
    """Writes nothing for `PAUSE_S` before the turn's first step answers, as a long
    thinking block does."""

    PAUSE_S = 12.0

    async def call(
        self,
        request: ModelRequest,
        *,
        on_stream_event: StreamEventCallback | None = None,
        heartbeat: HeartbeatCallback | None = None,
    ) -> ModelResult:
        if request.key.step_idx == 0:
            await asyncio.sleep(self.PAUSE_S)
        return await super().call(request, on_stream_event=on_stream_event, heartbeat=heartbeat)


async def test_a_long_silence_keeps_the_turn_typing(
    app: FastAPI,
    app_settings: Settings,
    db_sessionmaker: Sessionmaker,
    runtime: ModelRuntime,
) -> None:
    model = _Thinks.from_paths(app_settings.cassette_dir)
    stage = await _stage(app, app_settings, db_sessionmaker)
    agents = _agents(runtime, app_settings, db_sessionmaker, model)

    async with _streams(
        app, stage, {"helen": VIEWERS["helen"], "alex": VIEWERS["alex"]}
    ) as streams:
        result = await _run_turn(agents, stage)
        received = {
            name: await streams[name].envelopes_until(_is_message, WAIT_S)
            for name in ("helen", "alex")
        }

    assert result.stop == "end_turn"
    helen = _stream_events(received["helen"])
    # Until the model writes, the stream is typing frames alone: the opening one, then
    # the heartbeat at 5 s and 10 s into the 12 s silence.
    silent = list(takewhile(lambda event: isinstance(event, StreamTyping), helen))
    assert len(silent) >= 3, helen
    alex = _stream_events(received["alex"])
    assert len(alex) >= 3, alex
    assert all(isinstance(event, StreamTyping) for event in alex), alex


async def test_running_a_finished_turn_again_reuses_it(
    app: FastAPI,
    app_settings: Settings,
    db_sessionmaker: Sessionmaker,
    runtime: ModelRuntime,
) -> None:
    """The turn id names the turn: running it again must find the first run's calls
    and message, not take the turn a second time."""
    model = runtime.client
    assert isinstance(model, ScriptedModel)
    stage = await _stage(app, app_settings, db_sessionmaker)
    agents = _agents(runtime, app_settings, db_sessionmaker, model)
    first = await _run_turn(agents, stage)
    again = await _run_turn(agents, stage, attempt=2)

    assert (first.stop, again.stop) == ("end_turn", "end_turn")
    assert model.call_count == 2
    assert await _turn_rows(db_sessionmaker, stage) == (1, 2, 2)


def test_the_cassette_covers_the_turn(app_settings: Settings) -> None:
    """The worker's cassettes include this test's turn (and load without conflicts)."""
    cassettes = Path(app_settings.cassette_dir)
    assert (cassettes / "rep_turn.yaml").is_file()
    ScriptedModel.from_paths(cassettes)
