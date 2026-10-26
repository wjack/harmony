"""Hosted spend caps in Postgres (`budgets`, `budget_reservations`; §14).

These caps are not world-scoped: keys name a session lineage, an access code, a day
or the live network run, so Reset never refills them. A reservation is atomic across
all of its keys: rows are locked in key order (no deadlocks between reservations that
share keys) and each is bumped only `WHERE spent + reserved + est <= cap`. A key with
no row fails closed. Settling and releasing are idempotent: they act only on a
reservation still in `reserved`.

`hosted_budget_keys` is what a model activity passes as `budget_keys`: with
`HostedCaps` (live calls in the hosted demo) it names the world's session lineage (and
the day, once a daily cap is set) and creates those rows on first use, so every hosted
call is metered per lineage and a Reset, which keeps the lineage, never refills it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.config import Settings
from harmony.db.models import Budget, BudgetReservation, World
from harmony.llm.errors import BudgetExhausted

LIVE_NETWORK_KEY: Final = "live-network"


def lineage_key(lineage: UUID) -> str:
    return f"lineage:{lineage}"


def access_key(code_hash: str) -> str:
    return f"access:{code_hash}"


def daily_key(day: date) -> str:
    return f"daily:{day.isoformat()}"


async def ensure_budget(session: AsyncSession, key: str, cap_usd: Decimal) -> None:
    """Create the budget row if it doesn't exist (an existing cap is left alone)."""
    await session.execute(
        pg_insert(Budget)
        .values(key=key, cap_usd=cap_usd)
        .on_conflict_do_nothing(index_elements=["key"])
    )


@dataclass(frozen=True, slots=True)
class HostedCaps:
    """The in-app caps a hosted live call is reserved against (§14)."""

    lineage_usd: Decimal
    daily_usd: Decimal | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> HostedCaps | None:
        """Caps for live calls with APP_ENV=prod; None elsewhere (local dev is capped by
        the $200 ledger, and scripted calls cost nothing)."""
        if settings.app_env != "prod" or settings.model_mode != "live":
            return None
        return cls(lineage_usd=settings.lineage_budget_usd, daily_usd=settings.daily_budget_usd)


async def hosted_budget_keys(
    sessionmaker: async_sessionmaker[AsyncSession],
    caps: HostedCaps | None,
    *,
    world_id: UUID,
    today: date | None = None,
) -> tuple[str, ...]:
    """The hosted budget keys for a model call in `world_id`; () without `caps`.

    Creates each key's row with its cap if it is missing (an existing row, and its
    spend, is left alone). A world that is gone fails closed.
    """
    if caps is None:
        return ()
    async with sessionmaker() as session, session.begin():
        lineage = await session.scalar(select(World.budget_lineage).where(World.id == world_id))
        if lineage is None:
            raise BudgetExhausted(
                f"world {world_id} has no budget lineage; refusing an uncapped call",
                budget="lineage",
                cap_usd=Decimal(0),
                committed_usd=Decimal(0),
                requested_usd=Decimal(0),
            )
        caps_by_key = {lineage_key(lineage): caps.lineage_usd}
        if caps.daily_usd is not None:
            caps_by_key[daily_key(today or datetime.now(UTC).date())] = caps.daily_usd
        for key, cap in caps_by_key.items():
            await ensure_budget(session, key, cap)
    return tuple(caps_by_key)


class HostedBudgets:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def reserve(self, reservation_id: UUID, keys: Sequence[str], est_usd: Decimal) -> None:
        ordered = sorted(set(keys))
        async with self._sessionmaker() as session, session.begin():
            rows = (
                await session.execute(
                    select(Budget.key, Budget.cap_usd, Budget.spent_usd, Budget.reserved_usd)
                    .where(Budget.key.in_(ordered))
                    .order_by(Budget.key)
                    .with_for_update()
                )
            ).all()
            found = {row.key: row for row in rows}
            for key in ordered:
                row = found.get(key)
                if row is None:
                    raise BudgetExhausted(
                        f"no budget row for {key!r}; refusing an uncapped call",
                        budget=key,
                        cap_usd=Decimal(0),
                        committed_usd=Decimal(0),
                        requested_usd=est_usd,
                    )
                committed = row.spent_usd + row.reserved_usd
                if committed + est_usd > row.cap_usd:
                    raise BudgetExhausted(
                        f"budget {key!r} exhausted: ${committed} committed of ${row.cap_usd}, "
                        f"call needs up to ${est_usd}",
                        budget=key,
                        cap_usd=row.cap_usd,
                        committed_usd=committed,
                        requested_usd=est_usd,
                    )
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
                    budget=",".join(ordered),
                    cap_usd=Decimal(0),
                    committed_usd=Decimal(0),
                    requested_usd=est_usd,
                )
            session.add(
                BudgetReservation(
                    id=reservation_id, keys=ordered, est_usd=est_usd, status="reserved"
                )
            )

    async def settle(self, reservation_id: UUID, cost_usd: Decimal) -> None:
        await self._close(reservation_id, spent_usd=cost_usd)

    async def release(self, reservation_id: UUID) -> None:
        await self._close(reservation_id, spent_usd=None)

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
            await self._close(reservation_id, spent_usd=None, at_estimate=True)
        return len(stale)

    async def _close(
        self, reservation_id: UUID, *, spent_usd: Decimal | None, at_estimate: bool = False
    ) -> None:
        """Close a `reserved` reservation: settle `spent_usd`, settle its estimate
        (`at_estimate`), or release it (neither)."""
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
                return
            keys, est_usd = closed
            if at_estimate:
                spent_usd = est_usd
            ordered = sorted(keys)
            await session.execute(
                select(Budget.key)
                .where(Budget.key.in_(ordered))
                .order_by(Budget.key)
                .with_for_update()
            )
            await session.execute(
                update(Budget)
                .where(Budget.key.in_(ordered))
                .values(
                    reserved_usd=Budget.reserved_usd - est_usd,
                    spent_usd=Budget.spent_usd + (spent_usd or Decimal(0)),
                    updated_at=func.now(),
                )
                .execution_options(synchronize_session=False)
            )


__all__ = [
    "LIVE_NETWORK_KEY",
    "HostedBudgets",
    "HostedCaps",
    "access_key",
    "daily_key",
    "ensure_budget",
    "hosted_budget_keys",
    "lineage_key",
]
