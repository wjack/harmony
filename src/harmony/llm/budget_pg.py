"""Hosted spend caps in Postgres (`budgets`, `budget_reservations`, `live_controls`;
LIVE_DEMO_PLAN 3.4 and 3.6).

The keys are not world-scoped, so Reset never refills one:
- `lineage:<uuid>`: a browser session's lineage, which every Reset and fork keeps
  (`LINEAGE_BUDGET_USD`);
- `client:<hash>:<UTC date>`: a known client network's day, from the session's
  `client_key_hash` (`CLIENT_DAILY_BUDGET_USD`);
- `daily:<UTC date>`: the day (`DAILY_BUDGET_USD`);
- `global`: the whole demo (`GLOBAL_BUDGET_USD`).

Every hosted reservation first reads the live switch (`live_controls`, key `live`):
paused, or no row at all, refuses with kind `paused`, and an active provider block with
kind `provider`, before any budget row is locked. It then reserves the call's worst
case against all of its keys at once: rows are locked in key order (no deadlocks
between reservations that share keys) and each is bumped only
`WHERE spent + reserved + est <= cap`. A key with no row fails closed. When several
caps refuse, the widest is named (`REFUSAL_ORDER`), so the visitor is never promised a
refill that wouldn't let them continue.

Settling and releasing are idempotent: they act only on a reservation still
`reserved`. A failed one is retried, then left for `settle_stale`. A settle that takes
the global or a daily row past 50, 75, 90 or 100% of its cap logs `hosted_spend_crossed`
once, under the row lock, naming the day's top lineage and client.

`ensure_hosted_rows` (API and worker boot) inserts the live switch if it is missing and
sets the global and today's daily caps from configuration, which is their one source.
`hosted_budget_keys` creates missing lineage, client and daily rows with the current
caps; a lineage or client row keeps the cap it was created with.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.config import Settings
from harmony.db.models import Budget, BudgetReservation, LiveControls, VisitorSession, World
from harmony.db.models.choices import BudgetKind
from harmony.llm.errors import BudgetExhausted

log = logging.getLogger(__name__)

GLOBAL_KEY: Final = "global"
LIVE_SWITCH_KEY: Final = "live"
# After the provider's own spend limit refuses a call, hosted calls are refused locally
# for this long; the first call after it probes the provider again.
PROVIDER_BLOCK: Final = timedelta(minutes=10)
# A cap counts as used up with less than this left: no production-mix call fits under
# it (a test pins it against the builders' worst cases at a 20,000-token input).
EXHAUSTED_BELOW_USD: Final = Decimal("1.10")
# Percentages of the global and daily caps that log `hosted_spend_crossed`.
CROSSING_PCTS: Final = (50, 75, 90, 100)
# Waits between the attempts to close a reservation; then it is left for the sweep.
SETTLE_RETRY_DELAYS_S: Final = (0.5, 1.0, 2.0)
# Which refusal is reported when several apply: the one that lifts last, or for the most
# people. A paused or blocked demo, then a spent demo, then a spent session (Reset
# never refills it), then a day that resets at midnight.
REFUSAL_ORDER: Final[tuple[BudgetKind, ...]] = (
    "paused",
    "provider",
    "global",
    "session",
    "daily",
    "client",
)
_KIND_BY_PREFIX: Final[Mapping[str, BudgetKind]] = {
    "lineage": "session",
    "client": "client",
    "daily": "daily",
    GLOBAL_KEY: "global",
}
_DB_ERRORS: Final = (SQLAlchemyError, OSError)
_ZERO: Final = Decimal(0)


# --- Keys ---------------------------------------------------------------------------------


def lineage_key(lineage: UUID) -> str:
    return f"lineage:{lineage}"


def client_budget_key(client_key_hash: str, day: date) -> str:
    return f"client:{client_key_hash}:{day.isoformat()}"


def daily_key(day: date) -> str:
    return f"daily:{day.isoformat()}"


def key_kind(key: str) -> BudgetKind:
    """Which cap a budget key is, as `BudgetExhausted.kind` names it."""
    return _KIND_BY_PREFIX.get(key.split(":", 1)[0], "global")


def widest(kinds: Iterable[BudgetKind]) -> BudgetKind | None:
    """The refusal to report among several (`REFUSAL_ORDER`); None for none."""
    return min(kinds, key=REFUSAL_ORDER.index, default=None)


def utc_today() -> date:
    return datetime.now(UTC).date()


def next_utc_midnight(day: date) -> datetime:
    """When `day`'s daily and client caps reset."""
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC)


async def ensure_budget(session: AsyncSession, key: str, cap_usd: Decimal) -> None:
    """Create the budget row if it doesn't exist (an existing cap is left alone)."""
    await session.execute(
        pg_insert(Budget)
        .values(key=key, cap_usd=cap_usd)
        .on_conflict_do_nothing(index_elements=["key"])
    )


@dataclass(frozen=True, slots=True)
class HostedCaps:
    """The in-app caps a hosted live call is reserved against (3.4). Unset caps (local
    runs) have no key."""

    lineage_usd: Decimal
    client_daily_usd: Decimal | None = None
    daily_usd: Decimal | None = None
    global_usd: Decimal | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> HostedCaps | None:
        """Caps for live calls with APP_ENV=prod, or locally with HOSTED_CAPS_IN_DEV; None
        elsewhere (local dev is capped by the $200 ledger, and scripted calls are free)."""
        hosted = settings.app_env == "prod" or (
            settings.app_env == "local" and settings.hosted_caps_in_dev
        )
        if settings.model_mode != "live" or not hosted:
            return None
        return cls(
            lineage_usd=settings.lineage_budget_usd,
            client_daily_usd=settings.client_daily_budget_usd,
            daily_usd=settings.daily_budget_usd,
            global_usd=settings.global_budget_usd,
        )

    def caps_by_key(
        self, *, lineage: UUID, client_key_hash: str | None, day: date
    ) -> dict[str, Decimal]:
        """The keys a call for this lineage reserves against on `day`, with their caps.
        A session from an unknown client key has no client key."""
        caps = {lineage_key(lineage): self.lineage_usd}
        if client_key_hash is not None and self.client_daily_usd is not None:
            caps[client_budget_key(client_key_hash, day)] = self.client_daily_usd
        if self.daily_usd is not None:
            caps[daily_key(day)] = self.daily_usd
        if self.global_usd is not None:
            caps[GLOBAL_KEY] = self.global_usd
        return caps


async def hosted_budget_keys(
    sessionmaker: async_sessionmaker[AsyncSession],
    caps: HostedCaps | None,
    *,
    world_id: UUID,
    today: date | None = None,
) -> tuple[str, ...]:
    """The hosted budget keys for a model call in `world_id`; () without `caps`.

    Reads the session's client key through world → lineage → session, and creates each
    missing lineage, client and daily row with its cap (an existing row, and its spend,
    is left alone). The global row comes only from configuration at boot, so a missing
    one refuses rather than restarting the demo's total at zero. A world that is gone
    fails closed.
    """
    if caps is None:
        return ()
    async with sessionmaker() as session, session.begin():
        lineage = await session.scalar(select(World.budget_lineage).where(World.id == world_id))
        if lineage is None:
            raise BudgetExhausted(
                f"world {world_id} has no budget lineage; refusing an uncapped call",
                kind="session",
                budget="lineage",
                cap_usd=_ZERO,
                committed_usd=_ZERO,
                requested_usd=_ZERO,
            )
        caps_by_key = caps.caps_by_key(
            lineage=lineage,
            client_key_hash=await _client_key_hash(session, lineage),
            day=today or utc_today(),
        )
        for key, cap in caps_by_key.items():
            if key != GLOBAL_KEY:
                await ensure_budget(session, key, cap)
    return tuple(caps_by_key)


async def _client_key_hash(session: AsyncSession, lineage: UUID) -> str | None:
    return await session.scalar(
        select(VisitorSession.client_key_hash)
        .where(
            VisitorSession.budget_lineage == lineage,
            VisitorSession.client_key_hash.is_not(None),
        )
        .order_by(VisitorSession.created_at)
        .limit(1)
    )


async def ensure_hosted_rows(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """At boot (API and worker): insert the live switch if it is missing (unpaused; an
    existing row keeps its state), and with hosted caps on, upsert the `global` and
    today's `daily:` rows with the configured caps, so a changed cap applies at the next
    boot. Spend is never touched."""
    caps = HostedCaps.from_settings(settings)
    async with sessionmaker() as session, session.begin():
        await session.execute(
            pg_insert(LiveControls)
            .values(key=LIVE_SWITCH_KEY)
            .on_conflict_do_nothing(index_elements=["key"])
        )
        if caps is None:
            return
        configured = {GLOBAL_KEY: caps.global_usd, daily_key(utc_today()): caps.daily_usd}
        # In key order, as reservations and settles lock rows: a boot during live calls
        # must not lock `global` before `daily:` and deadlock with them.
        for key, cap in sorted(configured.items()):
            if cap is not None:
                insert = pg_insert(Budget).values(key=key, cap_usd=cap)
                await session.execute(
                    insert.on_conflict_do_update(
                        index_elements=["key"],
                        set_={"cap_usd": insert.excluded.cap_usd, "updated_at": func.now()},
                    )
                )


# --- The live switch ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Totals:
    cap_usd: Decimal
    spent_usd: Decimal
    reserved_usd: Decimal

    @property
    def remaining_usd(self) -> Decimal:
        """The cap less settled spend. Calls in flight hold `reserved_usd` of it until
        they settle, nearly always for less."""
        return max(self.cap_usd - self.spent_usd, _ZERO)

    @property
    def exhausted(self) -> bool:
        return self.remaining_usd < EXHAUSTED_BELOW_USD


@dataclass(frozen=True, slots=True)
class KeyTotals(Totals):
    key: str


@dataclass(frozen=True, slots=True)
class LiveReport:
    """The live switch and the demo's total, as the operator sees them."""

    # True when the switch row is missing too: reservations then refuse.
    paused: bool
    provider_blocked_until: datetime | None
    # The `global` row; None when no global cap is configured.
    global_: Totals | None


@dataclass(frozen=True, slots=True)
class _Switch:
    paused: bool
    provider_blocked_until: datetime | None
    # `provider_blocked_until` is in the future (the database's clock).
    provider_blocked: bool


async def _read_switch(session: AsyncSession) -> _Switch | None:
    row = (
        await session.execute(
            select(
                LiveControls.paused,
                LiveControls.provider_blocked_until,
                func.coalesce(LiveControls.provider_blocked_until > func.now(), False),
            ).where(LiveControls.key == LIVE_SWITCH_KEY)
        )
    ).one_or_none()
    if row is None:
        return None
    paused, until, blocked = row
    return _Switch(paused=paused, provider_blocked_until=until, provider_blocked=bool(blocked))


async def _live_report(session: AsyncSession) -> LiveReport:
    switch = await _read_switch(session)
    totals = await _totals(session, [GLOBAL_KEY])
    return LiveReport(
        paused=switch is None or switch.paused,
        provider_blocked_until=switch.provider_blocked_until if switch is not None else None,
        global_=totals.get(GLOBAL_KEY),
    )


async def set_live(sessionmaker: async_sessionmaker[AsyncSession], *, enabled: bool) -> LiveReport:
    """Pause or resume live models (an upsert, so it works on a database that has never
    seen a live call). Resuming also clears a provider block. No cap is touched."""
    async with sessionmaker() as session, session.begin():
        insert = pg_insert(LiveControls).values(key=LIVE_SWITCH_KEY, paused=not enabled)
        changes: dict[str, object] = {"paused": not enabled, "updated_at": func.now()}
        if enabled:
            changes["provider_blocked_until"] = None
        await session.execute(insert.on_conflict_do_update(index_elements=["key"], set_=changes))
        report = await _live_report(session)
    log.warning("live models %s by the operator", "resumed" if enabled else "paused")
    return report


# --- What a visitor and the operator see ------------------------------------------------


@dataclass(frozen=True, slots=True)
class VisitorBudget:
    lineage: Totals
    # What stops this visitor's live calls now, widest first; None while all have room.
    stopped_by: BudgetKind | None


async def visitor_budget(
    sessionmaker: async_sessionmaker[AsyncSession],
    caps: HostedCaps,
    *,
    session_id: UUID,
    today: date | None = None,
) -> VisitorBudget:
    """The session's lineage and which cap, if any, has less than `EXHAUSTED_BELOW_USD`
    left. A missing lineage, client or daily row is a full cap (its first call creates
    it); a missing global row refuses, as a reservation would."""
    async with sessionmaker() as session:
        lineage, client_key_hash = (
            await session.execute(
                select(VisitorSession.budget_lineage, VisitorSession.client_key_hash).where(
                    VisitorSession.id == session_id
                )
            )
        ).one()
        caps_by_key = caps.caps_by_key(
            lineage=lineage, client_key_hash=client_key_hash, day=today or utc_today()
        )
        switch = await _read_switch(session)
        rows = await _totals(session, caps_by_key)
    stopped: list[BudgetKind] = []
    if switch is None or switch.paused:
        stopped.append("paused")
    elif switch.provider_blocked:
        stopped.append("provider")
    for key in caps_by_key:
        row = rows.get(key)
        if (row is None and key == GLOBAL_KEY) or (row is not None and row.exhausted):
            stopped.append(key_kind(key))
    own = lineage_key(lineage)
    return VisitorBudget(
        lineage=rows.get(own) or Totals(caps_by_key[own], _ZERO, _ZERO),
        stopped_by=widest(stopped),
    )


@dataclass(frozen=True, slots=True)
class SpendReport:
    live: LiveReport
    # Today's `daily:` row, if there is one yet.
    today: Totals | None
    # Lineages with a reservation or settle today, and the top ones by their spend.
    lineages_today: int
    top_lineages: list[KeyTotals]
    # Today's client rows, by spend.
    top_clients: list[KeyTotals]


async def spend_report(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    today: date | None = None,
    lineages: int = 10,
    clients: int = 5,
) -> SpendReport:
    day = today or utc_today()
    async with sessionmaker() as session:
        live = await _live_report(session)
        daily = (await _totals(session, [daily_key(day)])).get(daily_key(day))
        active = _lineages_active_on(day)
        count = await session.scalar(select(func.count()).select_from(Budget).where(active))
        return SpendReport(
            live=live,
            today=daily,
            lineages_today=count or 0,
            top_lineages=await _top(session, active, lineages),
            top_clients=await _top(session, _clients_on(day), clients),
        )


def _lineages_active_on(day: date) -> ColumnElement[bool]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return Budget.key.startswith("lineage:") & (Budget.updated_at >= start)


def _clients_on(day: date) -> ColumnElement[bool]:
    return Budget.key.startswith("client:") & Budget.key.endswith(f":{day.isoformat()}")


async def _top(session: AsyncSession, where: ColumnElement[bool], limit: int) -> list[KeyTotals]:
    rows = await session.execute(
        select(Budget.key, Budget.cap_usd, Budget.spent_usd, Budget.reserved_usd)
        .where(where)
        .order_by(Budget.spent_usd.desc(), Budget.key)
        .limit(limit)
    )
    return [
        KeyTotals(key=key, cap_usd=cap, spent_usd=spent, reserved_usd=reserved)
        for key, cap, spent, reserved in rows
    ]


async def _totals(session: AsyncSession, keys: Iterable[str]) -> dict[str, Totals]:
    rows = await session.execute(
        select(Budget.key, Budget.cap_usd, Budget.spent_usd, Budget.reserved_usd).where(
            Budget.key.in_(list(keys))
        )
    )
    return {
        key: Totals(cap_usd=cap, spent_usd=spent, reserved_usd=reserved)
        for key, cap, spent, reserved in rows
    }


# --- Reserving and settling -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Crossing:
    key: str
    pct: int
    spent_usd: Decimal
    cap_usd: Decimal


def _crossing_level(cap: Decimal, pct: int) -> Decimal:
    if pct < 100:
        return cap * pct / 100
    # Reservations stop spend short of the cap by up to a call's worst case, so the cap
    # is full once it reads as used up (or at 90% on a cap too small for that).
    return max(cap - EXHAUSTED_BELOW_USD, cap * Decimal("0.9"))


def _crossings(key: str, cap: Decimal, before: Decimal, after: Decimal) -> list[_Crossing]:
    if key != GLOBAL_KEY and key_kind(key) != "daily":
        return []
    return [
        _Crossing(key, pct, after, cap)
        for pct in CROSSING_PCTS
        if before < _crossing_level(cap, pct) <= after
    ]


class HostedBudgets:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        settle_retry_delays_s: Sequence[float] = SETTLE_RETRY_DELAYS_S,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._retry_delays_s = tuple(settle_retry_delays_s)

    async def reserve(self, reservation_id: UUID, keys: Sequence[str], est_usd: Decimal) -> None:
        ordered = sorted(set(keys))
        async with self._sessionmaker() as session, session.begin():
            _refuse_unless_live(await _read_switch(session), est_usd)
            rows = (
                await session.execute(
                    select(Budget.key, Budget.cap_usd, Budget.spent_usd, Budget.reserved_usd)
                    .where(Budget.key.in_(ordered))
                    .order_by(Budget.key)
                    .with_for_update()
                )
            ).all()
            found = {
                key: Totals(cap_usd=cap, spent_usd=spent, reserved_usd=reserved)
                for key, cap, spent, reserved in rows
            }
            refusals = [
                refusal
                for key in ordered
                if (refusal := _refusal(key, found.get(key), est_usd)) is not None
            ]
            if refusals:
                raise min(refusals, key=lambda refusal: REFUSAL_ORDER.index(refusal.kind))
            bumped = (
                await session.execute(
                    update(Budget)
                    .where(
                        Budget.key.in_(ordered),
                        Budget.spent_usd + Budget.reserved_usd + est_usd <= Budget.cap_usd,
                    )
                    .values(reserved_usd=Budget.reserved_usd + est_usd, updated_at=func.now())
                    .returning(Budget.key)
                    .execution_options(synchronize_session=False)
                )
            ).all()
            if len(bumped) != len(ordered):  # unreachable while the rows are locked
                raise BudgetExhausted(
                    "hosted budget changed during reservation",
                    kind=key_kind(ordered[0]),
                    budget=",".join(ordered),
                    cap_usd=_ZERO,
                    committed_usd=_ZERO,
                    requested_usd=est_usd,
                )
            session.add(
                BudgetReservation(
                    id=reservation_id, keys=ordered, est_usd=est_usd, status="reserved"
                )
            )

    async def settle(self, reservation_id: UUID, cost_usd: Decimal) -> None:
        await self._close_retrying(reservation_id, spent_usd=cost_usd)

    async def release(self, reservation_id: UUID) -> None:
        await self._close_retrying(reservation_id, spent_usd=None)

    async def block_provider(self, duration: timedelta = PROVIDER_BLOCK) -> None:
        """The provider's own spend limit refused a call: refuse hosted calls locally for
        `duration`. A missing switch row already refuses everything, so only an existing
        row is updated."""
        async with self._sessionmaker() as session, session.begin():
            await session.execute(
                update(LiveControls)
                .where(LiveControls.key == LIVE_SWITCH_KEY)
                .values(provider_blocked_until=func.now() + duration, updated_at=func.now())
            )
        log.warning(
            "the provider's spend limit refused a call; live calls blocked for %s", duration
        )

    async def settle_stale(self, older_than: timedelta) -> int:
        """Close reservations left `reserved` by a process that died mid-call.

        Such a call was most likely billed, and what it cost is unknown, so each is
        settled at its estimate (the worst case it reserved): the caps over-count a
        crash rather than let its spend be spent again. Never settle with an
        `older_than` shorter than a call can run (the model activity's timeout); a
        live call's own settle afterwards is a no-op.
        """
        async with self._sessionmaker() as session:
            stale = (
                await session.scalars(
                    select(BudgetReservation.id).where(
                        BudgetReservation.status == "reserved",
                        BudgetReservation.created_at < func.now() - older_than,
                    )
                )
            ).all()
        for reservation_id in stale:
            await self._report(await self._close(reservation_id, spent_usd=None, at_estimate=True))
        return len(stale)

    async def _close_retrying(self, reservation_id: UUID, *, spent_usd: Decimal | None) -> None:
        """Close a reservation, retrying database failures. One that keeps failing stays
        `reserved` until `settle_stale` settles it at its estimate: an over-count, never
        headroom handed out twice. The call's own result is not failed for it."""
        delays = iter(self._retry_delays_s)
        attempt = 1
        while True:
            try:
                crossings = await self._close(reservation_id, spent_usd=spent_usd)
            except _DB_ERRORS as exc:
                delay = next(delays, None)
                if delay is None:
                    log.error(
                        "closing hosted reservation %s failed %d times; settle_stale will "
                        "settle it at its estimate: %r",
                        reservation_id,
                        attempt,
                        exc,
                    )
                    return
                log.warning(
                    "closing hosted reservation %s failed (attempt %d): %r",
                    reservation_id,
                    attempt,
                    exc,
                )
                attempt += 1
                await asyncio.sleep(delay)
            else:
                await self._report(crossings)
                return

    async def _close(
        self, reservation_id: UUID, *, spent_usd: Decimal | None, at_estimate: bool = False
    ) -> list[_Crossing]:
        """Close a `reserved` reservation: settle `spent_usd`, settle its estimate
        (`at_estimate`), or release it (neither). Returns the cap crossings it caused,
        computed under the rows' locks, so each crossing is seen by one close only."""
        if at_estimate:
            status, settled = "settled", BudgetReservation.est_usd
        elif spent_usd is None:
            status, settled = "released", None
        else:
            status, settled = "settled", spent_usd
        async with self._sessionmaker() as session, session.begin():
            closed = (
                await session.execute(
                    update(BudgetReservation)
                    .where(
                        BudgetReservation.id == reservation_id,
                        BudgetReservation.status == "reserved",
                    )
                    .values(status=status, settled_usd=settled, settled_at=func.now())
                    .returning(BudgetReservation.keys, BudgetReservation.est_usd)
                    .execution_options(synchronize_session=False)
                )
            ).one_or_none()
            if closed is None:
                return []
            keys, est_usd = closed
            cost = est_usd if at_estimate else spent_usd or _ZERO
            ordered = sorted(keys)
            before = (
                await session.execute(
                    select(Budget.key, Budget.cap_usd, Budget.spent_usd)
                    .where(Budget.key.in_(ordered))
                    .order_by(Budget.key)
                    .with_for_update()
                )
            ).all()
            crossings = [
                crossing
                for key, cap, spent in before
                for crossing in _crossings(key, cap, spent, spent + cost)
            ]
            await session.execute(
                update(Budget)
                .where(Budget.key.in_(ordered))
                .values(
                    reserved_usd=Budget.reserved_usd - est_usd,
                    spent_usd=Budget.spent_usd + cost,
                    updated_at=func.now(),
                )
                .execution_options(synchronize_session=False)
            )
        return crossings

    async def _report(self, crossings: Sequence[_Crossing]) -> None:
        """Log each crossing, with who is spending most today, so the operator can see
        one actor draining the day (3.6)."""
        if not crossings:
            return
        day = utc_today()
        try:
            async with self._sessionmaker() as session:
                lineages = await _top(session, _lineages_active_on(day), 1)
                clients = await _top(session, _clients_on(day), 1)
        except _DB_ERRORS as exc:
            log.warning("could not read today's top spenders: %r", exc)
            lineages, clients = [], []
        top_lineage = lineages[0].key if lineages else "none"
        top_client = clients[0].key.split(":")[1] if clients else "none"
        for crossing in crossings:
            log.warning(
                "hosted_spend_crossed key=%s pct=%d spent_usd=%s cap_usd=%s top_lineage=%s "
                "top_client=%s",
                crossing.key,
                crossing.pct,
                crossing.spent_usd,
                crossing.cap_usd,
                top_lineage,
                top_client,
            )


def _refuse_unless_live(switch: _Switch | None, est_usd: Decimal) -> None:
    if switch is None or switch.paused:
        raise BudgetExhausted(
            "live models are paused" if switch is not None else "the live switch row is missing",
            kind="paused",
            budget=LIVE_SWITCH_KEY,
            cap_usd=_ZERO,
            committed_usd=_ZERO,
            requested_usd=est_usd,
        )
    if switch.provider_blocked:
        raise BudgetExhausted(
            f"the provider's spend limit refused a call; live calls are blocked until "
            f"{switch.provider_blocked_until}",
            kind="provider",
            budget=LIVE_SWITCH_KEY,
            cap_usd=_ZERO,
            committed_usd=_ZERO,
            requested_usd=est_usd,
        )


def _refusal(key: str, row: Totals | None, est_usd: Decimal) -> BudgetExhausted | None:
    if row is None:
        return BudgetExhausted(
            f"no budget row for {key!r}; refusing an uncapped call",
            kind=key_kind(key),
            budget=key,
            cap_usd=_ZERO,
            committed_usd=_ZERO,
            requested_usd=est_usd,
        )
    committed = row.spent_usd + row.reserved_usd
    if committed + est_usd <= row.cap_usd:
        return None
    return BudgetExhausted(
        f"budget {key!r} exhausted: ${committed} committed of ${row.cap_usd}, "
        f"call needs up to ${est_usd}",
        kind=key_kind(key),
        budget=key,
        cap_usd=row.cap_usd,
        committed_usd=committed,
        requested_usd=est_usd,
    )


__all__ = [
    "CROSSING_PCTS",
    "EXHAUSTED_BELOW_USD",
    "GLOBAL_KEY",
    "LIVE_SWITCH_KEY",
    "PROVIDER_BLOCK",
    "REFUSAL_ORDER",
    "HostedBudgets",
    "HostedCaps",
    "KeyTotals",
    "LiveReport",
    "SpendReport",
    "Totals",
    "VisitorBudget",
    "client_budget_key",
    "daily_key",
    "ensure_budget",
    "ensure_hosted_rows",
    "hosted_budget_keys",
    "key_kind",
    "lineage_key",
    "next_utc_midnight",
    "set_live",
    "spend_report",
    "utc_today",
    "visitor_budget",
    "widest",
]
