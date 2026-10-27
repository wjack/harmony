"""Hosted spend caps in Postgres (LIVE_DEMO_PLAN 3.4, 3.6): the four keys, the live
switch and the provider block, boot-time caps, the settle retry and the threshold logs.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.db.models import Budget, BudgetReservation, LiveControls, VisitorSession, World
from harmony.db.models.choices import BudgetKind
from harmony.llm import CallKey
from harmony.llm.budget import BudgetGuard, spend_guard
from harmony.llm.budget_pg import (
    GLOBAL_KEY,
    LIVE_SWITCH_KEY,
    HostedBudgets,
    HostedCaps,
    client_budget_key,
    daily_key,
    ensure_budget,
    ensure_hosted_rows,
    hosted_budget_keys,
    lineage_key,
    set_live,
    utc_today,
)
from harmony.llm.errors import BudgetExhausted
from harmony.llm.ledger import SpendLedger
from harmony.llm.pricing import OPUS_5_5
from tests.conftest import prod_settings, settings_without_dotenv

type Sessionmaker = async_sessionmaker[AsyncSession]

CAPS = HostedCaps(
    lineage_usd=Decimal(10),
    client_daily_usd=Decimal(25),
    daily_usd=Decimal(60),
    global_usd=Decimal(250),
)
DAY = date(2026, 10, 26)
CLIENT_HASH = "c" * 32
LOG = "harmony.llm.budget_pg"


async def _world(sm: Sessionmaker, *, client_key_hash: str | None = None) -> tuple[UUID, UUID]:
    """A world and its session sharing one lineage; returns (world id, lineage)."""
    world_id, lineage = uuid4(), uuid4()
    async with sm() as session, session.begin():
        session.add(
            World(
                id=world_id,
                scenario="harmony-demo",
                mode="live",
                build_sha="test",
                budget_lineage=lineage,
            )
        )
        session.add(
            VisitorSession(id=uuid4(), budget_lineage=lineage, client_key_hash=client_key_hash)
        )
    return world_id, lineage


async def _rows(sm: Sessionmaker) -> dict[str, tuple[Decimal, Decimal, Decimal]]:
    async with sm() as session:
        rows = await session.execute(
            select(Budget.key, Budget.cap_usd, Budget.spent_usd, Budget.reserved_usd)
        )
        return {key: (cap, spent, reserved) for key, cap, spent, reserved in rows}


async def _make(sm: Sessionmaker, caps: dict[str, Decimal]) -> None:
    async with sm() as session, session.begin():
        for key, cap in caps.items():
            await ensure_budget(session, key, cap)


async def _spend(sm: Sessionmaker, key: str, spent: Decimal) -> None:
    async with sm() as session, session.begin():
        await session.execute(update(Budget).where(Budget.key == key).values(spent_usd=spent))


async def _drop_switch(sm: Sessionmaker) -> None:
    async with sm() as session, session.begin():
        await session.execute(delete(LiveControls))


async def _reservations(sm: Sessionmaker) -> dict[UUID, tuple[str, Decimal | None]]:
    async with sm() as session:
        return {
            row.id: (row.status, row.settled_usd)
            for row in await session.scalars(select(BudgetReservation))
        }


async def _refusal(hosted: HostedBudgets, keys: Sequence[str], est: Decimal) -> BudgetExhausted:
    with pytest.raises(BudgetExhausted) as refused:
        await hosted.reserve(uuid4(), keys, est)
    return refused.value


def _four_keys(lineage: UUID) -> dict[str, Decimal]:
    return CAPS.caps_by_key(lineage=lineage, client_key_hash=CLIENT_HASH, day=DAY)


# --- Keys -----------------------------------------------------------------------------------


async def test_a_call_reserves_against_four_keys_and_their_rows_are_created(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    world_id, lineage = await _world(sm, client_key_hash=CLIENT_HASH)

    keys = await hosted_budget_keys(sm, CAPS, world_id=world_id, today=DAY)

    client, daily = client_budget_key(CLIENT_HASH, DAY), daily_key(DAY)
    assert keys == (lineage_key(lineage), client, daily, GLOBAL_KEY)
    # The global row comes from configuration at boot, never from a call.
    assert await _rows(sm) == {
        lineage_key(lineage): (Decimal(10), Decimal(0), Decimal(0)),
        client: (Decimal(25), Decimal(0), Decimal(0)),
        daily: (Decimal(60), Decimal(0), Decimal(0)),
    }


async def test_a_session_without_a_client_key_reserves_against_three(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id, lineage = await _world(db_sessionmaker)
    keys = await hosted_budget_keys(db_sessionmaker, CAPS, world_id=world_id, today=DAY)
    assert keys == (lineage_key(lineage), daily_key(DAY), GLOBAL_KEY)
    assert await hosted_budget_keys(db_sessionmaker, None, world_id=world_id) == ()


async def test_an_existing_row_keeps_its_cap_and_spend(db_sessionmaker: Sessionmaker) -> None:
    sm = db_sessionmaker
    world_id, lineage = await _world(sm)
    await _make(sm, {lineage_key(lineage): Decimal(6)})
    await _spend(sm, lineage_key(lineage), Decimal(2))
    await hosted_budget_keys(sm, CAPS, world_id=world_id, today=DAY)
    assert (await _rows(sm))[lineage_key(lineage)] == (Decimal(6), Decimal(2), Decimal(0))


async def test_a_world_that_is_gone_fails_closed(db_sessionmaker: Sessionmaker) -> None:
    with pytest.raises(BudgetExhausted) as refused:
        await hosted_budget_keys(db_sessionmaker, CAPS, world_id=uuid4())
    assert refused.value.kind == "session"


# --- Refusals name their cap ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spent_key", "kind"),
    [("lineage", "session"), ("client", "client"), ("daily", "daily"), ("global", "global")],
)
async def test_each_exhausted_key_names_its_kind(
    db_sessionmaker: Sessionmaker, spent_key: str, kind: BudgetKind
) -> None:
    sm = db_sessionmaker
    lineage = uuid4()
    caps = _four_keys(lineage)
    await _make(sm, caps)
    full = next(key for key in caps if key.startswith(spent_key))
    await _spend(sm, full, caps[full] - Decimal("0.5"))

    refused = await _refusal(HostedBudgets(sm), list(caps), Decimal(1))

    assert (refused.kind, refused.budget) == (kind, full)
    assert await _reservations(sm) == {}
    assert {reserved for _, _, reserved in (await _rows(sm)).values()} == {Decimal(0)}


async def test_the_widest_refusal_is_named(db_sessionmaker: Sessionmaker) -> None:
    sm = db_sessionmaker
    lineage = uuid4()
    caps = _four_keys(lineage)
    await _make(sm, caps)
    hosted = HostedBudgets(sm)
    # Reset never refills a session, so it outranks a day that resets at midnight.
    for key in (daily_key(DAY), client_budget_key(CLIENT_HASH, DAY), lineage_key(lineage)):
        await _spend(sm, key, caps[key])
    assert (await _refusal(hosted, list(caps), Decimal(1))).kind == "session"
    # A spent demo outranks them all.
    await _spend(sm, GLOBAL_KEY, caps[GLOBAL_KEY])
    assert (await _refusal(hosted, list(caps), Decimal(1))).kind == "global"


async def test_a_missing_row_refuses_as_its_kind(db_sessionmaker: Sessionmaker) -> None:
    hosted = HostedBudgets(db_sessionmaker)
    missing = await _refusal(hosted, [lineage_key(uuid4())], Decimal(1))
    assert missing.kind == "session"
    assert "no budget row" in str(missing)
    assert (await _refusal(hosted, [GLOBAL_KEY], Decimal(1))).kind == "global"


# --- The live switch ------------------------------------------------------------------------


async def test_without_the_switch_row_nothing_is_reserved(db_sessionmaker: Sessionmaker) -> None:
    sm = db_sessionmaker
    await _drop_switch(sm)
    assert await _rows(sm) == {}

    refused = await _refusal(HostedBudgets(sm), [lineage_key(uuid4()), GLOBAL_KEY], Decimal(1))

    assert (refused.kind, refused.budget) == ("paused", LIVE_SWITCH_KEY)
    assert await _rows(sm) == {}
    assert await _reservations(sm) == {}


async def test_pause_creates_a_missing_row_and_resume_never_touches_a_cap(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    await _drop_switch(sm)
    paused = await set_live(sm, enabled=False)
    assert (paused.paused, paused.provider_blocked_until, paused.global_) == (True, None, None)
    assert await set_live(sm, enabled=False) == paused

    lineage = lineage_key(uuid4())
    await _make(sm, {lineage: Decimal(10), GLOBAL_KEY: Decimal(250)})
    await _spend(sm, GLOBAL_KEY, Decimal(7))
    hosted = HostedBudgets(sm)
    assert (await _refusal(hosted, [lineage, GLOBAL_KEY], Decimal(1))).kind == "paused"
    before = await _rows(sm)

    resumed = await set_live(sm, enabled=True)
    assert not resumed.paused
    assert resumed.global_ is not None
    assert (resumed.global_.cap_usd, resumed.global_.spent_usd) == (Decimal(250), Decimal(7))
    assert await set_live(sm, enabled=True) == resumed
    assert await _rows(sm) == before
    await hosted.reserve(uuid4(), [lineage, GLOBAL_KEY], Decimal(1))


async def test_a_provider_block_refuses_until_resumed(db_sessionmaker: Sessionmaker) -> None:
    sm = db_sessionmaker
    lineage = lineage_key(uuid4())
    await _make(sm, {lineage: Decimal(10)})
    hosted = HostedBudgets(sm)

    await hosted.block_provider()
    refused = await _refusal(hosted, [lineage], Decimal(1))
    assert refused.kind == "provider"
    blocked = await set_live(sm, enabled=False)
    assert blocked.provider_blocked_until is not None

    resumed = await set_live(sm, enabled=True)
    assert resumed.provider_blocked_until is None
    await hosted.reserve(uuid4(), [lineage], Decimal(1))


async def test_an_expired_provider_block_lets_the_next_call_probe(
    db_sessionmaker: Sessionmaker,
) -> None:
    lineage = lineage_key(uuid4())
    await _make(db_sessionmaker, {lineage: Decimal(10)})
    hosted = HostedBudgets(db_sessionmaker)
    await hosted.block_provider(timedelta(seconds=-1))
    await hosted.reserve(uuid4(), [lineage], Decimal(1))


# --- Boot -------------------------------------------------------------------------------------


async def test_boot_sets_the_configured_caps_and_keeps_spend(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    await _drop_switch(sm)
    settings = prod_settings(daily_budget_usd=Decimal(60), global_budget_usd=Decimal(250))
    today = daily_key(utc_today())

    await ensure_hosted_rows(sm, settings)

    rows = await _rows(sm)
    assert (rows[GLOBAL_KEY], rows[today]) == (
        (Decimal(250), Decimal(0), Decimal(0)),
        (Decimal(60), Decimal(0), Decimal(0)),
    )
    async with sm() as session:
        switch = await session.get_one(LiveControls, LIVE_SWITCH_KEY)
        assert not switch.paused

    lineage = lineage_key(uuid4())
    await _make(sm, {lineage: Decimal(10)})
    await _spend(sm, GLOBAL_KEY, Decimal(12))
    await set_live(sm, enabled=False)
    raised = settings.model_copy(
        update={"global_budget_usd": Decimal(300), "lineage_budget_usd": Decimal(20)}
    )
    await ensure_hosted_rows(sm, raised)

    rows = await _rows(sm)
    assert rows[GLOBAL_KEY] == (Decimal(300), Decimal(12), Decimal(0))
    assert rows[lineage] == (Decimal(10), Decimal(0), Decimal(0))
    async with sm() as session:
        assert (await session.get_one(LiveControls, LIVE_SWITCH_KEY)).paused


async def test_boot_locks_its_rows_in_key_order_as_reservations_do(
    db_sessionmaker: Sessionmaker,
) -> None:
    """A boot during live calls queues behind a settle that has `daily:` locked; it must
    not lock `global` meanwhile, or the next reservation (daily, then global) deadlocks
    with it."""
    sm = db_sessionmaker
    settings = prod_settings(daily_budget_usd=Decimal(60), global_budget_usd=Decimal(250))
    await ensure_hosted_rows(sm, settings)
    today = daily_key(utc_today())

    async with sm() as holder, holder.begin():
        await holder.execute(select(Budget).where(Budget.key == today).with_for_update())
        boot = asyncio.create_task(ensure_hosted_rows(sm, settings))
        await asyncio.sleep(0.5)  # the boot is now waiting on `daily:`
        assert not boot.done()
        async with sm() as other, other.begin():
            locked = await other.execute(
                select(Budget.key).where(Budget.key == GLOBAL_KEY).with_for_update(nowait=True)
            )
            assert locked.scalars().all() == [GLOBAL_KEY]
    await asyncio.wait_for(boot, timeout=10)


async def test_boot_without_hosted_caps_writes_only_the_switch(
    db_sessionmaker: Sessionmaker,
) -> None:
    await _drop_switch(db_sessionmaker)
    await ensure_hosted_rows(db_sessionmaker, settings_without_dotenv(model_mode="scripted"))
    assert await _rows(db_sessionmaker) == {}
    async with db_sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(LiveControls)) == 1


async def test_hosted_caps_in_dev_reserve_against_the_ledger_and_postgres(
    tmp_path: Path, db_sessionmaker: Sessionmaker
) -> None:
    sm = db_sessionmaker
    ledger = tmp_path / "spend_ledger.jsonl"
    ledger.write_text("")
    settings = settings_without_dotenv(
        model_mode="live",
        app_env="local",
        spend_ledger_path=ledger,
        hosted_caps_in_dev=True,
        daily_budget_usd=Decimal(60),
        global_budget_usd=Decimal(250),
    )
    caps = HostedCaps.from_settings(settings)
    assert caps is not None
    await ensure_hosted_rows(sm, settings)
    world_id, lineage = await _world(sm)
    keys = await hosted_budget_keys(sm, caps, world_id=world_id)
    guard = spend_guard(settings, sessionmaker=sm)
    assert isinstance(guard, BudgetGuard)

    reservation = await guard.reserve_amount(
        Decimal(2), key=_call_key(world_id), model=OPUS_5_5, budget_keys=keys
    )

    with SpendLedger(ledger).locked() as locked:
        assert [r.est_usd for r in locked.open_reservations().values()] == [Decimal(2)]
    assert {key: reserved for key, (_, _, reserved) in (await _rows(sm)).items()} == {
        lineage_key(lineage): Decimal(2),
        daily_key(utc_today()): Decimal(2),
        GLOBAL_KEY: Decimal(2),
    }
    await guard.settle(
        reservation, cost_usd=Decimal("0.5"), usage={}, served_model=OPUS_5_5, stop_reason=None
    )
    assert SpendLedger(ledger).read().spent_usd == Decimal("0.5")
    assert (await _rows(sm))[GLOBAL_KEY] == (Decimal(250), Decimal("0.5"), Decimal(0))


def _call_key(world_id: UUID) -> CallKey:
    return CallKey(
        world_id=world_id,
        scenario="test",
        org="northlake",
        role="internal",
        thread=None,
        turn=1,
        turn_id=uuid4(),
        step_idx=0,
        attempt=1,
    )


# --- Settling ---------------------------------------------------------------------------------


class _FlakySessions:
    """A sessionmaker whose next `failures` sessions can't be opened."""

    def __init__(self, sm: Sessionmaker) -> None:
        self._sm = sm
        self.failures = 0
        self.calls = 0

    def __call__(self) -> AsyncSession:
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise OSError("connection reset by peer")
        return self._sm()


async def test_a_settle_that_fails_twice_is_retried_and_settled_once(
    db_sessionmaker: Sessionmaker,
) -> None:
    lineage = lineage_key(uuid4())
    await _make(db_sessionmaker, {lineage: Decimal(10)})
    flaky = _FlakySessions(db_sessionmaker)
    hosted = HostedBudgets(cast(Sessionmaker, flaky), settle_retry_delays_s=(0, 0, 0))
    reservation = uuid4()
    await hosted.reserve(reservation, [lineage], Decimal(3))

    flaky.failures = 2
    await hosted.settle(reservation, Decimal("1.5"))
    await hosted.settle(reservation, Decimal("1.5"))

    assert await _reservations(db_sessionmaker) == {reservation: ("settled", Decimal("1.5"))}
    assert (await _rows(db_sessionmaker))[lineage] == (Decimal(10), Decimal("1.5"), Decimal(0))


async def test_a_settle_that_keeps_failing_is_left_for_the_sweep(
    db_sessionmaker: Sessionmaker, caplog: pytest.LogCaptureFixture
) -> None:
    lineage = lineage_key(uuid4())
    await _make(db_sessionmaker, {lineage: Decimal(10)})
    flaky = _FlakySessions(db_sessionmaker)
    hosted = HostedBudgets(cast(Sessionmaker, flaky), settle_retry_delays_s=(0, 0, 0))
    reservation = uuid4()
    await hosted.reserve(reservation, [lineage], Decimal(3))

    flaky.failures = 10
    with caplog.at_level(logging.ERROR, logger=LOG):
        await hosted.settle(reservation, Decimal("1.5"))
    assert flaky.calls == 1 + 4  # the reservation, then the first try and three retries
    assert "settle_stale will settle it" in caplog.text
    assert await _reservations(db_sessionmaker) == {reservation: ("reserved", None)}

    flaky.failures = 0
    assert await HostedBudgets(db_sessionmaker).settle_stale(timedelta(0)) == 1
    assert await _reservations(db_sessionmaker) == {reservation: ("settled", Decimal(3))}
    assert (await _rows(db_sessionmaker))[lineage] == (Decimal(10), Decimal(3), Decimal(0))


# --- Threshold logs ---------------------------------------------------------------------------

_CROSSED = re.compile(
    r"hosted_spend_crossed key=(\S+) pct=(\d+) .* top_lineage=(\S+) top_client=(\S+)"
)


async def test_each_threshold_is_logged_once_under_concurrency(
    db_sessionmaker: Sessionmaker, caplog: pytest.LogCaptureFixture
) -> None:
    sm = db_sessionmaker
    heavy, light = lineage_key(uuid4()), lineage_key(uuid4())
    client = client_budget_key(CLIENT_HASH, utc_today())
    await _make(
        sm,
        {GLOBAL_KEY: Decimal(100), heavy: Decimal(100), light: Decimal(100), client: Decimal(100)},
    )
    hosted = HostedBudgets(sm)

    async def spend(calls: int, *, lineage: str) -> None:
        ids = [uuid4() for _ in range(calls)]
        await asyncio.gather(
            *(hosted.reserve(i, [GLOBAL_KEY, lineage, client], Decimal(3)) for i in ids)
        )
        await asyncio.gather(*(hosted.settle(i, Decimal(3)) for i in ids))

    with caplog.at_level(logging.WARNING, logger=LOG):
        await spend(24, lineage=heavy)  # $72: 50%
        await spend(6, lineage=light)  # $90: 75% and 90%, in concurrent settles
        await spend(3, lineage=heavy)  # $99: under EXHAUSTED_BELOW_USD left, so 100%

    crossed = [m.groups() for m in map(_CROSSED.search, caplog.messages) if m is not None]
    assert sorted(int(pct) for _, pct, _, _ in crossed) == [50, 75, 90, 100]
    assert {(key, lineage, top) for key, _, lineage, top in crossed} == {
        (GLOBAL_KEY, heavy, CLIENT_HASH)
    }
    # Lineage and client rows are not alerted on; only the demo's and the day's totals.
    assert (await _rows(sm))[GLOBAL_KEY] == (Decimal(100), Decimal(99), Decimal(0))
