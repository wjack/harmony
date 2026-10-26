"""`call_model_checkpointed`: at most one paid call per (turn_id, step_idx) (§5).

1. If `model_calls` has a row for the key, return it: no call, no spend.
2. Otherwise reserve the call's worst case, make the call, insert the raw request and
   response in their own transaction (`ON CONFLICT DO NOTHING`), and settle what it
   billed. The insert happens before any post-processing, so a crash after this
   point retries into step 1.
3. If the call fails, settle the usage billed before the failure (from the error, or
   from the stream events seen so far) and re-raise; a call that billed nothing just
   releases its reservation. Cancellation (activity heartbeat timeout, termination,
   worker shutdown) is settled the same way. A stream that broke off before its
   `message_delta` never reported its output tokens, so it is settled as if it had
   generated `max_tokens` of them: the ledger may overcount a failed call, never
   undercount it.

`ScriptedModel` must run with `FreeSpend` and every other client with a real guard,
so a scripted run can never write ledger lines and a paid call can never go
unmetered.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.db.models import ModelCall
from harmony.llm import (
    HeartbeatCallback,
    ModelClient,
    ModelRequest,
    ModelResult,
    StreamEventCallback,
)
from harmony.llm.budget import FreeSpend, Reservation, SpendGuard
from harmony.llm.errors import ModelCallError
from harmony.llm.scripted import ScriptedModel
from harmony.llm.usage import StreamUsageTracker
from harmony.llm.wire import JsonObject, as_int, as_str, canonical_json

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CheckpointedCall:
    """A stored model call. `reused` is True when no call was made by this attempt."""

    turn_id: UUID
    step_idx: int
    world_id: UUID
    request_text: str
    response_text: str
    served_model: str
    usage: dict[str, Any]
    cost_usd: Decimal
    attempt: int
    reused: bool

    def message(self) -> dict[str, Any]:
        """The final message as parsed JSON (content blocks in API shape)."""
        parsed: dict[str, Any] = json.loads(self.response_text)
        return parsed

    @property
    def stop_reason(self) -> str | None:
        reason = self.message().get("stop_reason")
        return reason if isinstance(reason, str) else None

    @classmethod
    def from_row(cls, row: ModelCall, *, reused: bool) -> CheckpointedCall:
        return cls(
            turn_id=row.turn_id,
            step_idx=row.step_idx,
            world_id=row.world_id,
            request_text=row.request_text,
            response_text=row.response_text,
            served_model=row.served_model,
            usage=dict(row.usage_json),
            cost_usd=row.cost_usd,
            attempt=row.attempt,
            reused=reused,
        )


class GuardMismatch(TypeError):
    """A client paired with the wrong spend guard."""


async def call_model_checkpointed(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: ModelClient,
    guard: SpendGuard,
    request: ModelRequest,
    *,
    on_stream_event: StreamEventCallback | None = None,
    heartbeat: HeartbeatCallback | None = None,
    budget_keys: Sequence[str] = (),
) -> CheckpointedCall:
    key = request.key
    request_text = canonical_json(request.body)
    stored = await load_call(sessionmaker, key.turn_id, key.step_idx)
    if stored is not None:
        if stored.world_id != key.world_id:
            raise ValueError(
                f"model call ({key.turn_id}, {key.step_idx}) belongs to world "
                f"{stored.world_id}, not {key.world_id}"
            )
        if stored.request_text != request_text:
            log.warning(
                "reusing stored response for (%s, %s) although the rebuilt request differs",
                key.turn_id,
                key.step_idx,
            )
        return stored

    async def store(result: ModelResult, cost: Decimal) -> CheckpointedCall:
        return await _insert(sessionmaker, request, request_text, result, cost)

    return await metered_call(
        client,
        guard,
        request,
        store,
        on_stream_event=on_stream_event,
        heartbeat=heartbeat,
        budget_keys=budget_keys,
    )


async def metered_call[T](
    client: ModelClient,
    guard: SpendGuard,
    request: ModelRequest,
    store: Callable[[ModelResult, Decimal], Awaitable[T]],
    *,
    on_stream_event: StreamEventCallback | None = None,
    heartbeat: HeartbeatCallback | None = None,
    budget_keys: Sequence[str] = (),
) -> T:
    """Reserve, call, `store` the result, settle. Settles partial usage (or releases)
    when the call fails, and settles the full cost even when `store` fails."""
    _check_pairing(client, guard)
    requested_model = as_str(request.body.get("model")) or ""
    reservation = await guard.reserve(request, budget_keys=budget_keys)
    tracker = StreamUsageTracker()

    async def observe(event: Mapping[str, JsonValue]) -> None:
        tracker.observe(event)
        if on_stream_event is not None:
            await on_stream_event(event)

    try:
        result = await client.call(request, on_stream_event=observe, heartbeat=heartbeat)
    except BaseException as exc:
        await asyncio.shield(
            _settle_failure(
                guard,
                reservation,
                requested_model,
                tracker,
                exc,
                max_output_tokens=as_int(request.body.get("max_tokens")),
            )
        )
        raise

    cost = guard.price(
        result.usage, requested_model=requested_model, served_model=result.served_model
    )
    try:
        return await store(result, cost)
    finally:
        # Shielded: a cancelled activity must still record what the call cost.
        await asyncio.shield(
            guard.settle(
                reservation,
                cost_usd=cost,
                usage=result.usage,
                served_model=result.served_model,
                stop_reason=result.stop_reason,
            )
        )


async def load_call(
    sessionmaker: async_sessionmaker[AsyncSession], turn_id: UUID, step_idx: int
) -> CheckpointedCall | None:
    async with sessionmaker() as session:
        row = await session.get(ModelCall, (turn_id, step_idx))
        return CheckpointedCall.from_row(row, reused=True) if row is not None else None


def _check_pairing(client: ModelClient, guard: SpendGuard) -> None:
    scripted = isinstance(client, ScriptedModel)
    free = isinstance(guard, FreeSpend)
    if scripted and not free:
        raise GuardMismatch("ScriptedModel runs with FreeSpend; it must never touch the ledger")
    if free and not scripted:
        raise GuardMismatch(
            f"{type(client).__name__} needs a BudgetGuard; FreeSpend meters nothing"
        )


async def _settle_failure(
    guard: SpendGuard,
    reservation: Reservation,
    requested_model: str,
    tracker: StreamUsageTracker,
    exc: BaseException,
    *,
    max_output_tokens: int | None,
) -> None:
    usage = tracker.usage
    served_model = tracker.served_model
    if isinstance(exc, ModelCallError):
        usage = exc.partial_usage if exc.partial_usage is not None else usage
        served_model = exc.served_model or served_model
    if usage is None:
        await guard.release(reservation)
        return
    served = served_model or requested_model
    cost = guard.price(usage, requested_model=requested_model, served_model=served)
    if not tracker.output_reported and max_output_tokens is not None:
        # The output generated before the break was billed but never reported.
        bound = _with_output_tokens(usage, max_output_tokens)
        cost = max(cost, guard.price(bound, requested_model=requested_model, served_model=served))
    log.warning(
        "model call (%s, %s) failed after billing $%s: %r",
        reservation.key.turn_id,
        reservation.key.step_idx,
        cost,
        exc,
    )
    await guard.settle(
        reservation,
        cost_usd=cost,
        usage=usage,
        served_model=served,
        stop_reason=None,
        partial=True,
    )


def _with_output_tokens(usage: Mapping[str, JsonValue], tokens: int) -> JsonObject:
    """`usage` with at least `tokens` output tokens, priced from its top-level counts."""
    bounded: JsonObject = {key: value for key, value in usage.items() if key != "iterations"}
    bounded["output_tokens"] = max(as_int(usage.get("output_tokens")) or 0, tokens)
    return bounded


async def _insert(
    sessionmaker: async_sessionmaker[AsyncSession],
    request: ModelRequest,
    request_text: str,
    result: ModelResult,
    cost: Decimal,
) -> CheckpointedCall:
    key = request.key
    async with sessionmaker() as session, session.begin():
        inserted = await session.scalar(
            pg_insert(ModelCall)
            .values(
                turn_id=key.turn_id,
                step_idx=key.step_idx,
                world_id=key.world_id,
                request_text=request_text,
                response_text=result.response_text,
                served_model=result.served_model,
                usage_json=result.usage,
                cost_usd=cost,
                attempt=key.attempt,
            )
            .on_conflict_do_nothing(index_elements=["turn_id", "step_idx"])
            .returning(ModelCall.turn_id)
        )
        if inserted is not None:
            row = await session.get(ModelCall, (key.turn_id, key.step_idx))
            assert row is not None
            return CheckpointedCall.from_row(row, reused=False)
        # Another attempt stored this step first; its response is the one to use.
        existing = (
            await session.scalars(
                select(ModelCall).where(
                    ModelCall.turn_id == key.turn_id, ModelCall.step_idx == key.step_idx
                )
            )
        ).one()
        log.warning("(%s, %s) was stored by another attempt", key.turn_id, key.step_idx)
        return CheckpointedCall.from_row(existing, reused=True)


__all__ = [
    "CheckpointedCall",
    "GuardMismatch",
    "call_model_checkpointed",
    "load_call",
    "metered_call",
]
