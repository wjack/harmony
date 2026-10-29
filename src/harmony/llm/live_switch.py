"""The live switch in Postgres (`live_controls`, key `live`; LIVE_DEMO_PLAN 3.4).

There are no in-app spend caps: the Anthropic Console's workspace spend limit is the
hosted demo's backstop. Every live call from a guard with a database reads the switch
first (`LiveSwitch.check`): paused, or no row at all, refuses with kind `paused`, and an
active provider block with kind `provider`. The block is set when the Console's limit
refuses a call (`block_provider`), so later calls are refused here for `PROVIDER_BLOCK`
instead of each being sent to be refused again.

`ensure_live_switch` (API and worker boot) inserts the row if it is missing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.db.models import LiveControls
from harmony.db.models.choices import BudgetKind
from harmony.llm.errors import BudgetExhausted

log = logging.getLogger(__name__)

LIVE_SWITCH_KEY: Final = "live"
# After the provider's own spend limit refuses a call, live calls are refused locally
# for this long; the first call after it probes the provider again.
PROVIDER_BLOCK: Final = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class LiveReport:
    """The live switch, as the operator and the visitor's budget see it."""

    # True when the switch row is missing too: calls then refuse.
    paused: bool
    provider_blocked_until: datetime | None
    # `provider_blocked_until` is in the future (the database's clock).
    provider_blocked: bool = False

    @property
    def stopped_by(self) -> BudgetKind | None:
        """What refuses live calls now; None while they may run."""
        if self.paused:
            return "paused"
        return "provider" if self.provider_blocked else None


async def ensure_live_switch(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """At boot (API and worker): insert the live switch if it is missing, unpaused. An
    existing row keeps its state."""
    async with sessionmaker() as session, session.begin():
        await session.execute(
            pg_insert(LiveControls)
            .values(key=LIVE_SWITCH_KEY)
            .on_conflict_do_nothing(index_elements=["key"])
        )


async def live_report(sessionmaker: async_sessionmaker[AsyncSession]) -> LiveReport:
    async with sessionmaker() as session:
        return await _read(session)


async def set_live(sessionmaker: async_sessionmaker[AsyncSession], *, enabled: bool) -> LiveReport:
    """Pause or resume live models (an upsert, so it works on a database that has never
    seen a live call). Resuming also clears a provider block."""
    async with sessionmaker() as session, session.begin():
        insert = pg_insert(LiveControls).values(key=LIVE_SWITCH_KEY, paused=not enabled)
        changes: dict[str, object] = {"paused": not enabled, "updated_at": func.now()}
        if enabled:
            changes["provider_blocked_until"] = None
        await session.execute(insert.on_conflict_do_update(index_elements=["key"], set_=changes))
        report = await _read(session)
    log.warning("live models %s by the operator", "resumed" if enabled else "paused")
    return report


class LiveSwitch:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def check(self, est_usd: Decimal) -> None:
        """Raise `BudgetExhausted` unless live calls may run now."""
        async with self._sessionmaker() as session:
            report = await _read(session)
        if report.paused:
            raise BudgetExhausted(
                "live models are paused",
                kind="paused",
                budget=LIVE_SWITCH_KEY,
                cap_usd=Decimal(0),
                committed_usd=Decimal(0),
                requested_usd=est_usd,
            )
        if report.provider_blocked:
            raise BudgetExhausted(
                f"the provider's spend limit refused a call; live calls are blocked until "
                f"{report.provider_blocked_until}",
                kind="provider",
                budget=LIVE_SWITCH_KEY,
                cap_usd=Decimal(0),
                committed_usd=Decimal(0),
                requested_usd=est_usd,
            )

    async def block_provider(self, duration: timedelta = PROVIDER_BLOCK) -> None:
        """The provider's own spend limit refused a call: refuse live calls locally for
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


async def _read(session: AsyncSession) -> LiveReport:
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
        return LiveReport(paused=True, provider_blocked_until=None)
    paused, until, blocked = row
    return LiveReport(paused=paused, provider_blocked_until=until, provider_blocked=bool(blocked))


__all__ = [
    "LIVE_SWITCH_KEY",
    "PROVIDER_BLOCK",
    "LiveReport",
    "LiveSwitch",
    "ensure_live_switch",
    "live_report",
    "set_live",
]
