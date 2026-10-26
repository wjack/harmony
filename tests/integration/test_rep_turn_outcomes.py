"""The representative turn activity (`AgentActivities.rep_turn`), run directly.

- Hosted live calls are reserved against the session lineage's budget, which Reset
  keeps, so a visitor cannot refill their cap by resetting (§11.1, §14).
- Only a complete response is delivered: a response cut off at `max_tokens` fails the
  turn and commits nothing, and its text never streamed.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.testing import ActivityEnvironment

from harmony.agents.runtime import TurnIncomplete
from harmony.config import get_settings
from harmony.db.models import (
    Budget,
    NetworkRequest,
    Thread,
    ThreadMessage,
    VisitorSession,
    World,
)
from harmony.db.uow import unit_of_work
from harmony.events.bus import StreamMessage
from harmony.events.schemas import StreamTyping
from harmony.llm import (
    HeartbeatCallback,
    ModelRequest,
    ModelResult,
    StreamEventCallback,
)
from harmony.llm.budget import BudgetGuard, FreeSpend
from harmony.llm.budget_pg import HostedBudgets, HostedCaps, lineage_key
from harmony.llm.errors import BudgetExhausted
from harmony.llm.pricing import OPUS_5_5
from harmony.llm.scripted import CassetteEntry, CassetteKey, ScriptedModel
from harmony.temporal.activities.agents import AgentActivities, RepTurnInput
from harmony.temporal.policies import MODEL_NON_RETRYABLE
from harmony.worlds.retirer import SyncRetirer
from harmony.worlds.service import WorldService
from tests.conftest import settings_without_dotenv
from tests.integration._visitors import demo_id

type Sessionmaker = async_sessionmaker[AsyncSession]

HENDERSON, NORTHLAKE = demo_id("org", "henderson"), demo_id("org", "northlake")
THREAD_KEY = "rep-turn"  # tests/fixtures/cassettes/rep_turn.yaml


class _Publisher:
    def __init__(self) -> None:
        self.messages: list[StreamMessage] = []

    async def publish(self, message: StreamMessage) -> None:
        self.messages.append(message)

    async def aclose(self) -> None:
        return None


class _PaidModel:
    """Plays the cassettes like the live client would bill: it is not a `ScriptedModel`,
    so it runs under a real `BudgetGuard`. Nothing leaves the process."""

    def __init__(self, inner: ScriptedModel) -> None:
        self._inner = inner

    @property
    def call_count(self) -> int:
        return self._inner.call_count

    async def call(
        self,
        request: ModelRequest,
        *,
        on_stream_event: StreamEventCallback | None = None,
        heartbeat: HeartbeatCallback | None = None,
    ) -> ModelResult:
        return await self._inner.call(request, on_stream_event=on_stream_event, heartbeat=heartbeat)


async def _desk_world(sm: Sessionmaker, session_id: UUID) -> UUID:
    async with sm() as session:
        world_id = await session.scalar(
            select(VisitorSession.desk_world_id).where(VisitorSession.id == session_id)
        )
    assert world_id is not None
    return world_id


async def _stage(sm: Sessionmaker, world_id: UUID, *, thread_key: str = THREAD_KEY) -> RepTurnInput:
    """A Henderson -> Northlake thread whose next move is Northlake's representative."""
    thread_id, request_id = uuid4(), uuid4()
    async with unit_of_work(sm, world_id) as uow:
        uow.session.add(
            NetworkRequest(
                world_id=world_id,
                id=request_id,
                requester_org_id=HENDERSON,
                brief="Powder XRD on 12 air-sensitive cathode powders by Nov 6",
                created_by_member_id=demo_id("member", "alex"),
            )
        )
        uow.session.add(
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
        uow.session.add(
            ThreadMessage(
                world_id=world_id,
                id=uuid4(),
                thread_id=thread_id,
                seq=1,
                from_org_id=HENDERSON,
                kind="message",
                body="Can you run powder XRD on 12 air-sensitive samples by Nov 6?",
                provenance="agent",
            )
        )
        await uow.commit()
    return RepTurnInput(
        world_id=world_id,
        thread_id=thread_id,
        side="responder",
        org_id=NORTHLAKE,
        cassette_thread=thread_key,
        turn=1,
        turn_id=uuid4(),
    )


async def _northlake_messages(sm: Sessionmaker, turn: RepTurnInput) -> int:
    async with sm() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ThreadMessage)
            .where(
                ThreadMessage.world_id == turn.world_id,
                ThreadMessage.thread_id == turn.thread_id,
                ThreadMessage.from_org_id == NORTHLAKE,
            )
        )
    return count or 0


# --- Hosted caps: per session lineage, across Reset -----------------------------------


async def test_hosted_turns_are_capped_per_lineage_and_reset_never_refills_it(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    worlds = WorldService(sm, build_sha="test", retirer=SyncRetirer(sm))
    session_id = await worlds.start(None)
    world_id = await _desk_world(sm, session_id)
    async with sm() as session:
        lineage = await session.scalar(select(World.budget_lineage).where(World.id == world_id))
    assert lineage is not None

    # The hosted demo's live worker: no dev ledger, the Postgres caps only.
    model = _PaidModel(ScriptedModel.from_paths(get_settings().cassette_dir))
    guard = BudgetGuard(
        ledger=None,
        cap_usd=Decimal(200),
        thresholds=(Decimal(200),),
        env="prod",
        hosted=HostedBudgets(sm),
    )
    agents = AgentActivities(
        sm,
        model,
        guard,
        _Publisher(),
        model_override=OPUS_5_5,
        hosted_caps=HostedCaps(lineage_usd=Decimal(6)),
    )
    env = ActivityEnvironment()

    first = await env.run(agents.rep_turn, await _stage(sm, world_id))
    assert first.stop == "end_turn"
    calls = model.call_count
    async with sm() as session:
        row = await session.get_one(Budget, lineage_key(lineage))
    assert (row.cap_usd, row.reserved_usd) == (Decimal(6), Decimal(0))
    assert row.spent_usd > 0, "the turn was not metered against the lineage"

    # The lineage's cap is spent. Reset forks a new world under the same lineage, and
    # its turns are refused before any model call.
    async with sm() as session, session.begin():
        await session.execute(
            update(Budget)
            .where(Budget.key == lineage_key(lineage))
            .values(spent_usd=Budget.cap_usd)
        )
    switch = await worlds.fork_and_reset(session_id, "desk", observed_world_id=world_id)
    assert switch.world_id != world_id
    with pytest.raises(BudgetExhausted):
        await env.run(agents.rep_turn, await _stage(sm, switch.world_id))
    assert model.call_count == calls


def test_hosted_caps_apply_only_to_live_calls_in_prod() -> None:
    prod_live = settings_without_dotenv(app_env="prod", model_mode="live", session_secret="x" * 40)
    assert HostedCaps.from_settings(prod_live) == HostedCaps(lineage_usd=Decimal(6))
    assert HostedCaps.from_settings(prod_live.model_copy(update={"model_mode": "scripted"})) is None
    assert HostedCaps.from_settings(prod_live.model_copy(update={"app_env": "local"})) is None


# --- Only complete responses are delivered ----------------------------------------------


def _cut_off(content: list[Mapping[str, Any]]) -> ScriptedModel:
    key: CassetteKey = ("harmony-demo", "northlake", "representative", "cut-off", 1, 0)
    entry = CassetteEntry.model_validate(
        {
            "match": {"org": "northlake", "role": "representative", "thread": "cut-off", "turn": 1},
            "content": content,
            "stop_reason": "max_tokens",
        }
    )
    return ScriptedModel({key: entry})


PREAMBLE_THEN_CUT_TOOL: list[Mapping[str, JsonValue]] = [
    {"type": "text", "text": "Let me check with the team first."},
    # What the SDK stores for a send_message cut mid-body: the partial string is dropped.
    {"type": "tool_use", "name": "send_message", "input": {}},
]
CUT_TEXT: list[Mapping[str, JsonValue]] = [
    {"type": "text", "text": "Yes, we can meet Nov 6. The price would be $1,1"},
]


@pytest.mark.parametrize(
    "content", [PREAMBLE_THEN_CUT_TOOL, CUT_TEXT], ids=["preamble-then-cut-tool", "cut-text"]
)
async def test_a_response_cut_off_at_max_tokens_is_never_delivered(
    db_sessionmaker: Sessionmaker, content: list[Mapping[str, Any]]
) -> None:
    sm = db_sessionmaker
    worlds = WorldService(sm, build_sha="test", retirer=SyncRetirer(sm))
    world_id = await _desk_world(sm, await worlds.start(None))
    publisher = _Publisher()
    agents = AgentActivities(sm, _cut_off(content), FreeSpend(), publisher, model_override=OPUS_5_5)
    turn = await _stage(sm, world_id, thread_key="cut-off")

    with pytest.raises(TurnIncomplete):
        await ActivityEnvironment().run(agents.rep_turn, turn)

    # A retry would replay the same stored response.
    assert TurnIncomplete.__name__ in MODEL_NON_RETRYABLE
    assert await _northlake_messages(sm, turn) == 0
    # A preamble or a bare reply isn't streamed before it is known to be a message, so
    # Northlake's members saw only that the representative was typing.
    events = [message.event for message in publisher.messages]
    assert events
    assert all(isinstance(event, StreamTyping) for event in events), events
