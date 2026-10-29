"""Spend guards: reserve before a model call, settle what it billed after (§14).

`BudgetGuard` is the live guard. For every call it:
1. with a database, reads the live switch first (`live_switch`): paused, or blocked
   after the provider's own spend limit refused a call, refuses the call;
2. reserves the call's worst case (`pricing.worst_case_cost`) against the $200 dev
   cap, counting harmony's settled spend from the shared ledger plus every process's
   open reservations (the ledger's sidecar file), under an `asyncio.Lock` and an
   exclusive `flock`;
3. after the call, appends one `call` line with the billed cost (also for failed and
   cut-off calls, from their partial usage) and drops the open reservation under the
   same lock, and writes one `alert` line the first time spend reaches each threshold.

A reservation that would pass the cap raises the non-retryable `BudgetExhausted`
(kind `global`: the dev cap is this machine's whole demo); the first such refusal also
writes the cap's alert (reason `refused`), since spend can then never reach the cap
exactly. When the provider's own spend limit refuses a call, the model client calls
`block_provider`, and live calls are refused locally for a while (3.4). There are no
in-app caps in prod: the ledger is the dev machine's, and the hosted demo's backstop is
the Anthropic Console's workspace spend limit.

With `MODEL_MODE=live` and `APP_ENV=local` the ledger is mandatory: a missing,
unreadable or unwritable ledger refuses to start (`LedgerUnavailable`), and the guard
never creates it. Build guards with `spend_guard(settings)`, which returns
`FreeSpend` (no reservations, no ledger lines, $0) in scripted mode.

Open reservations are shared through the sidecar, not kept per process: the dev
worker and a `harmony llm ping --live` (or a second worker) each see the other's calls
in flight, so together they cannot pass the cap. `in_flight_usd` is this guard's own.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.config import Settings
from harmony.llm import CallKey, ModelRequest
from harmony.llm.errors import BudgetExhausted, LedgerUnavailable
from harmony.llm.ledger import (
    AlertReason,
    LedgerEntry,
    LockedLedger,
    OpenReservation,
    SpendLedger,
)
from harmony.llm.live_switch import LiveSwitch
from harmony.llm.pricing import call_cost, round_usd, usage_counts, web_searches, worst_case_cost
from harmony.llm.wire import as_str

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Reservation:
    id: UUID
    key: CallKey
    model_requested: str
    est_usd: Decimal


@dataclass(frozen=True, slots=True)
class Alert:
    threshold_usd: Decimal
    spent_usd: Decimal
    reason: str

    def __str__(self) -> str:
        return (
            f"harmony dev spend alert: ${self.threshold_usd:,.2f} threshold "
            f"({self.reason}; ${self.spent_usd:,.2f} spent)"
        )


class SpendGuard(Protocol):
    def price(
        self, usage: Mapping[str, JsonValue], *, requested_model: str, served_model: str
    ) -> Decimal:
        """What a call with this usage cost."""
        ...

    async def reserve(self, request: ModelRequest) -> Reservation:
        """Reserve the request's worst case, or raise `BudgetExhausted`."""
        ...

    async def settle(
        self,
        reservation: Reservation,
        *,
        cost_usd: Decimal,
        usage: Mapping[str, JsonValue],
        served_model: str,
        stop_reason: str | None,
        partial: bool = False,
    ) -> tuple[Alert, ...]:
        """Record what the call billed and free the reservation. Idempotent."""
        ...

    async def release(self, reservation: Reservation) -> None:
        """Free a reservation for a call that billed nothing. Idempotent."""
        ...

    async def block_provider(self) -> None:
        """The provider's spend limit refused a call: refuse live calls for a while."""
        ...


class FreeSpend:
    """The guard for `ScriptedModel`: everything is $0 and nothing is recorded."""

    def price(
        self, usage: Mapping[str, JsonValue], *, requested_model: str, served_model: str
    ) -> Decimal:
        return Decimal(0)

    async def reserve(self, request: ModelRequest) -> Reservation:
        model = as_str(request.body.get("model")) or ""
        return Reservation(uuid4(), request.key, model, Decimal(0))

    async def settle(
        self,
        reservation: Reservation,
        *,
        cost_usd: Decimal,
        usage: Mapping[str, JsonValue],
        served_model: str,
        stop_reason: str | None,
        partial: bool = False,
    ) -> tuple[Alert, ...]:
        return ()

    async def release(self, reservation: Reservation) -> None:
        return None

    async def block_provider(self) -> None:
        return None


class BudgetGuard:
    def __init__(
        self,
        *,
        ledger: SpendLedger | None,
        cap_usd: Decimal,
        thresholds: Sequence[Decimal],
        env: str,
        switch: LiveSwitch | None = None,
    ) -> None:
        self._ledger = ledger
        self._cap = cap_usd
        self._thresholds = tuple(sorted(set(thresholds)))
        self._env = env
        self._switch = switch
        self._lock = asyncio.Lock()
        self._open: dict[UUID, Reservation] = {}

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    ) -> BudgetGuard:
        """The live guard, reading the live switch through `sessionmaker` when given.
        Locally the ledger must exist and be readable and writable (checked now, so a
        worker without it refuses to start); it is never created."""
        ledger = None
        if settings.spend_ledger_path is not None:
            ledger = SpendLedger(settings.spend_ledger_path, project=settings.spend_project)
        if settings.app_env == "local" and ledger is None:
            raise LedgerUnavailable(
                "SPEND_LEDGER_PATH is not set; live calls in local development need the "
                "project's spend_ledger.jsonl"
            )
        if ledger is not None:
            ledger.check_usable()
            ledger.read()  # parses every line now: a corrupt ledger fails at startup
        return cls(
            ledger=ledger,
            cap_usd=settings.dev_budget_usd,
            thresholds=settings.alert_thresholds,
            env=settings.app_env,
            switch=LiveSwitch(sessionmaker) if sessionmaker is not None else None,
        )

    @property
    def in_flight_usd(self) -> Decimal:
        return sum((r.est_usd for r in self._open.values()), Decimal(0))

    def price(
        self, usage: Mapping[str, JsonValue], *, requested_model: str, served_model: str
    ) -> Decimal:
        return call_cost(usage, requested_model=requested_model, served_model=served_model)

    async def reserve(self, request: ModelRequest) -> Reservation:
        model = as_str(request.body.get("model")) or ""
        return await self.reserve_amount(
            worst_case_cost(request.body), key=request.key, model=model
        )

    async def reserve_amount(self, est_usd: Decimal, *, key: CallKey, model: str) -> Reservation:
        """Reserve an explicit amount (`reserve` computes it from the request)."""
        reservation = Reservation(uuid4(), key, model, round_usd(est_usd))
        if self._ledger is None and self._switch is None:
            raise BudgetExhausted(
                "nothing governs this call: no dev ledger and no live switch",
                kind="paused",
                budget="none",
                cap_usd=Decimal(0),
                committed_usd=Decimal(0),
                requested_usd=reservation.est_usd,
            )
        if self._switch is not None:
            await self._switch.check(reservation.est_usd)
        async with self._lock:
            if self._ledger is not None:
                await asyncio.to_thread(self._reserve_against_ledger, self._ledger, reservation)
            self._open[reservation.id] = reservation
        return reservation

    async def settle(
        self,
        reservation: Reservation,
        *,
        cost_usd: Decimal,
        usage: Mapping[str, JsonValue],
        served_model: str,
        stop_reason: str | None,
        partial: bool = False,
    ) -> tuple[Alert, ...]:
        cost = round_usd(cost_usd)
        entry = self._call_entry(reservation, cost, usage, served_model, stop_reason, partial)
        alerts: tuple[Alert, ...] = ()
        async with self._lock:
            if reservation.id not in self._open:
                return ()
            if self._ledger is not None:
                alerts = await asyncio.to_thread(
                    self._append_call, self._ledger, entry, reservation.id
                )
            # Only now: if the line couldn't be written, the reservation stays counted.
            del self._open[reservation.id]
        for alert in alerts:
            log.warning("%s", alert)
        return alerts

    async def release(self, reservation: Reservation) -> None:
        """Forget an open reservation here and in the shared sidecar."""
        async with self._lock:
            if reservation.id not in self._open:
                return
            if self._ledger is not None:
                await asyncio.to_thread(self._forget_open, self._ledger, reservation.id)
            del self._open[reservation.id]

    async def block_provider(self) -> None:
        """Without the live switch (`harmony llm ping`) there is nothing to block: the
        provider's error itself already failed the call."""
        if self._switch is not None:
            await self._switch.block_provider()

    # --- Ledger side (runs in a worker thread, under the asyncio lock) ---------------

    def _reserve_against_ledger(self, ledger: SpendLedger, reservation: Reservation) -> None:
        with ledger.locked() as locked:
            summary = locked.summary()
            open_reservations = locked.open_reservations()
            in_flight = sum((r.est_usd for r in open_reservations.values()), Decimal(0))
            committed = summary.spent_usd + in_flight
            if committed + reservation.est_usd <= self._cap:
                open_reservations[str(reservation.id)] = OpenReservation(
                    pid=os.getpid(), est_usd=reservation.est_usd, since=time.time()
                )
                locked.save_open_reservations(open_reservations)
                return
            refusals = [
                self._alert_entry(threshold, summary.spent_usd, "refused")
                for threshold in self._thresholds
                if threshold >= self._cap and threshold not in summary.alerted
            ]
            locked.append(refusals)
        for entry in refusals:
            log.warning("harmony dev budget exhausted; wrote %s alert", entry.threshold_usd)
        raise BudgetExhausted(
            f"dev budget exhausted: ${summary.spent_usd:,.2f} spent and "
            f"${in_flight:,.2f} in flight of ${self._cap:,.2f}; the call needs up "
            f"to ${reservation.est_usd:,.2f}",
            kind="global",
            budget="dev",
            cap_usd=self._cap,
            committed_usd=committed,
            requested_usd=reservation.est_usd,
        )

    def _append_call(
        self, ledger: SpendLedger, entry: LedgerEntry, reservation_id: UUID
    ) -> tuple[Alert, ...]:
        with ledger.locked() as locked:
            locked.append([entry])
            # Under the same lock: no reader sees the call both settled and in flight.
            locked.remove_open_reservation(str(reservation_id))
            return self._write_crossed_alerts(locked)

    def _forget_open(self, ledger: SpendLedger, reservation_id: UUID) -> None:
        with ledger.locked() as locked:
            locked.remove_open_reservation(str(reservation_id))

    def _write_crossed_alerts(self, locked: LockedLedger) -> tuple[Alert, ...]:
        summary = locked.summary()
        crossed = [
            threshold
            for threshold in self._thresholds
            if summary.spent_usd >= threshold and threshold not in summary.alerted
        ]
        locked.append([self._alert_entry(t, summary.spent_usd, "crossed") for t in crossed])
        return tuple(Alert(t, summary.spent_usd, "crossed") for t in crossed)

    # --- Lines ------------------------------------------------------------------------

    def _project(self, ledger: SpendLedger | None) -> str:
        return ledger.project if ledger is not None else "harmony"

    def _call_entry(
        self,
        reservation: Reservation,
        cost: Decimal,
        usage: Mapping[str, JsonValue],
        served_model: str,
        stop_reason: str | None,
        partial: bool,
    ) -> LedgerEntry:
        key = reservation.key
        tokens = usage_counts(usage)
        return LedgerEntry(
            project=self._project(self._ledger),
            kind="call",
            env=self._env,
            cost_usd=cost,
            world_id=str(key.world_id),
            role=key.role,
            model_requested=reservation.model_requested,
            model_served=served_model,
            turn_id=str(key.turn_id),
            step_idx=key.step_idx,
            attempt=key.attempt,
            input_tokens=tokens.input_tokens,
            output_tokens=tokens.output_tokens,
            cache_read_input_tokens=tokens.cache_read_input_tokens,
            cache_creation_input_tokens=tokens.cache_creation_input_tokens,
            cache_write_5m_tokens=tokens.cache_write_5m_tokens,
            cache_write_1h_tokens=tokens.cache_write_1h_tokens,
            web_searches=web_searches(usage),
            partial=partial or None,
            stop_reason=stop_reason,
        )

    def _alert_entry(self, threshold: Decimal, spent: Decimal, reason: AlertReason) -> LedgerEntry:
        return LedgerEntry(
            project=self._project(self._ledger),
            kind="alert",
            env=self._env,
            threshold_usd=threshold,
            spent_usd=spent,
            reason=reason,
        )


def spend_guard(
    settings: Settings, *, sessionmaker: async_sessionmaker[AsyncSession] | None = None
) -> SpendGuard:
    """The guard for this process's model mode. Call it at worker startup, so a live
    worker without its ledger refuses to start."""
    if settings.model_mode == "scripted":
        return FreeSpend()
    return BudgetGuard.from_settings(settings, sessionmaker=sessionmaker)


__all__ = [
    "Alert",
    "BudgetGuard",
    "FreeSpend",
    "Reservation",
    "SpendGuard",
    "spend_guard",
]
