"""What a run spent, and how fast its turns ran.

- The world's spend at each beat, from its checkpointed model calls.
- Its peak `spent + reserved`, approximated from the model calls.
- The same peak re-priced at production prices (3.14): each call's request is put back
  on its role's production model (`llm.requests.production_body`), each turn holds its
  largest re-priced call for as long as it ran, and settles its re-priced cost when it
  ends. A turn that keeps its largest call reserved throughout over-counts slightly, and the calls
  of a turn that never finished (no `agent_turns` row) aren't counted, so the figure is
  an approximation.
- Per-turn latency (p50 and p95), the models the calls asked for, the calls the model
  refused, and the process's peak RSS.
"""

from __future__ import annotations

import json
import math
import resource
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.agents import keys
from harmony.db.models import AgentTurn, Consult, ModelCall, Org
from harmony.db.models.choices import ModelRole
from harmony.domain.agent_turns import rev_turn_id
from harmony.llm.pricing import call_cost, worst_case_cost
from harmony.llm.requests import ROLE_PROFILES, Tier, production_body
from harmony.llm.wire import JsonObject, as_str

type Sessionmaker = async_sessionmaker[AsyncSession]


@dataclass(frozen=True, slots=True)
class CallRow:
    """One checkpointed model call, as stored."""

    request_text: str
    usage: Mapping[str, JsonValue]
    cost_usd: Decimal
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TurnCalls:
    """One agent turn's calls, with when it ran and what it ran as."""

    role: ModelRole
    tier: Tier
    started_at: datetime
    completed_at: datetime
    calls: tuple[CallRow, ...]


@dataclass(frozen=True, slots=True)
class Held:
    """A turn as a spend cap sees it: `hold_usd` reserved from start to end, then
    `settled_usd` spent."""

    started_at: datetime
    completed_at: datetime
    hold_usd: Decimal
    settled_usd: Decimal


@dataclass(frozen=True, slots=True)
class RunSpend:
    world_id: UUID
    calls: list[CallRow]
    turns: list[TurnCalls]
    latencies_ms: list[int]
    models: list[str]
    refusals: int


# --- Collecting -----------------------------------------------------------------------


async def run_spend(sessionmaker: Sessionmaker, world_id: UUID) -> RunSpend:
    async with sessionmaker() as session:
        calls = (
            await session.execute(
                select(
                    ModelCall.turn_id,
                    ModelCall.step_idx,
                    ModelCall.request_text,
                    ModelCall.response_text,
                    ModelCall.usage_json,
                    ModelCall.cost_usd,
                    ModelCall.created_at,
                )
                .where(ModelCall.world_id == world_id)
                .order_by(ModelCall.created_at)
            )
        ).all()
        turns = (
            await session.execute(
                select(
                    AgentTurn.id,
                    AgentTurn.role,
                    Org.featured,
                    AgentTurn.created_at,
                    AgentTurn.completed_at,
                    AgentTurn.latency_ms,
                )
                .join(Org, (Org.world_id == AgentTurn.world_id) & (Org.id == AgentTurn.org_id))
                .where(AgentTurn.world_id == world_id)
            )
        ).all()
        consult_ids = list(
            await session.scalars(select(Consult.id).where(Consult.world_id == world_id))
        )
    answer_turns = {keys.consult_turn_id(c) for c in consult_ids}
    sharing_turns = {keys.sharing_turn_id(c) for c in consult_ids}
    by_turn: dict[UUID, list[CallRow]] = {}
    rows: list[CallRow] = []
    refusals = 0
    for turn_id, step_idx, request_text, response_text, usage, cost, created_at in calls:
        row = CallRow(request_text, usage, cost, created_at)
        rows.append(row)
        refusals += _stop_reason(response_text) == "refusal"
        owner = _agent_turn_of(turn_id, step_idx, answer_turns, sharing_turns)
        by_turn.setdefault(owner, []).append(row)
    timed: list[TurnCalls] = []
    latencies: list[int] = []
    for turn_id, role, featured, created_at, completed_at, latency_ms in turns:
        if latency_ms is not None:
            latencies.append(latency_ms)
        end = completed_at or created_at
        start = end - timedelta(milliseconds=latency_ms or 0)
        tier: Tier = "featured" if featured else "background"
        timed.append(TurnCalls(role, tier, start, end, tuple(by_turn.get(turn_id, ()))))
    return RunSpend(world_id, rows, timed, latencies, models_requested(rows), refusals)


def _agent_turn_of(
    call_turn_id: UUID, step_idx: int, answer_turns: set[UUID], sharing_turns: set[UUID]
) -> UUID:
    """The `agent_turns` row a call is costed in: its own turn, or for a consult's
    answerer and sharing check, the row of the draft rev it belongs to."""
    if call_turn_id in answer_turns:
        return rev_turn_id(call_turn_id, step_idx // keys.ANSWERER_STEPS + 1)
    if call_turn_id in sharing_turns:
        return rev_turn_id(call_turn_id, step_idx + 1)
    return call_turn_id


def models_requested(calls: Iterable[CallRow]) -> list[str]:
    models = {as_str(_body(call).get("model")) for call in calls}
    return sorted(model for model in models if model is not None)


# --- Spend and peaks ------------------------------------------------------------------


def spent_by(calls: Iterable[CallRow], at: datetime) -> Decimal:
    """What the calls stored by `at` cost."""
    return sum((call.cost_usd for call in calls if call.created_at <= at), Decimal(0))


def as_measured(turn: TurnCalls) -> Held:
    """The turn at the prices it ran at."""
    hold = max((worst_case_cost(_body(call)) for call in turn.calls), default=Decimal(0))
    settled = sum((call.cost_usd for call in turn.calls), Decimal(0))
    return Held(turn.started_at, turn.completed_at, hold, settled)


def at_production_prices(turn: TurnCalls) -> Held:
    """The turn as it would have cost on its role's production model (3.14)."""
    model = ROLE_PROFILES[(turn.role, turn.tier)].model
    bodies = [production_body(_body(call), turn.role, turn.tier) for call in turn.calls]
    hold = max((worst_case_cost(body) for body in bodies), default=Decimal(0))
    settled = sum(
        (
            # Each attempt of a fallback chain names its own model; re-priced, every
            # token is the production model's.
            call_cost(_without_iterations(call.usage), requested_model=model, served_model=model)
            for call in turn.calls
        ),
        Decimal(0),
    )
    return Held(turn.started_at, turn.completed_at, hold, settled)


def peak_usd(turns: Iterable[TurnCalls], price: Callable[[TurnCalls], Held]) -> Decimal:
    """The highest `spent + reserved` over the run: settled spend so far plus the holds
    of the turns running at that moment. A turn ending as another starts isn't
    concurrent with it."""
    events: list[tuple[datetime, int, Decimal, Decimal]] = []
    for held in map(price, turns):
        events.append((held.started_at, 1, held.hold_usd, Decimal(0)))
        events.append((held.completed_at, 0, -held.hold_usd, held.settled_usd))
    events.sort(key=lambda event: (event[0], event[1]))
    reserved = settled = peak = Decimal(0)
    for _, _, hold, spent in events:
        reserved += hold
        settled += spent
        peak = max(peak, reserved + settled)
    return peak


def percentile_ms(values: Sequence[int], percent: float) -> int | None:
    """The nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * percent / 100))
    return ordered[rank - 1]


def peak_rss_mb() -> float:
    """This process's peak resident set: the in-process worker, the app and the player
    together. `ru_maxrss` is bytes on macOS and kilobytes on Linux."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def _body(call: CallRow) -> JsonObject:
    body: JsonValue = json.loads(call.request_text)
    return body if isinstance(body, dict) else {}


def _stop_reason(response_text: str) -> str | None:
    response: JsonValue = json.loads(response_text)
    return as_str(response.get("stop_reason")) if isinstance(response, dict) else None


def _without_iterations(usage: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {key: value for key, value in usage.items() if key != "iterations"}


__all__ = [
    "CallRow",
    "Held",
    "RunSpend",
    "TurnCalls",
    "as_measured",
    "at_production_prices",
    "models_requested",
    "peak_rss_mb",
    "peak_usd",
    "percentile_ms",
    "run_spend",
    "spent_by",
]
