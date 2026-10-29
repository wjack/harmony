"""The live switch (LIVE_DEMO_PLAN 3.4): pausing, the provider block, boot, and the live
guard reading it before the dev ledger."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.db.models import LiveControls
from harmony.llm import CallKey
from harmony.llm.budget import BudgetGuard, spend_guard
from harmony.llm.errors import BudgetExhausted
from harmony.llm.ledger import SpendLedger
from harmony.llm.live_switch import LIVE_SWITCH_KEY, LiveSwitch, ensure_live_switch, set_live
from harmony.llm.pricing import OPUS_5_5
from tests.conftest import settings_without_dotenv

type Sessionmaker = async_sessionmaker[AsyncSession]


async def _drop_switch(sm: Sessionmaker) -> None:
    async with sm() as session, session.begin():
        await session.execute(delete(LiveControls))


async def _refusal(switch: LiveSwitch) -> BudgetExhausted:
    with pytest.raises(BudgetExhausted) as refused:
        await switch.check(Decimal(1))
    return refused.value


async def test_without_the_switch_row_every_call_is_refused(db_sessionmaker: Sessionmaker) -> None:
    await _drop_switch(db_sessionmaker)
    refused = await _refusal(LiveSwitch(db_sessionmaker))
    assert (refused.kind, refused.budget) == ("paused", LIVE_SWITCH_KEY)


async def test_pause_creates_a_missing_row_and_resume_lets_calls_run(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    await _drop_switch(sm)
    paused = await set_live(sm, enabled=False)
    assert (paused.paused, paused.provider_blocked_until) == (True, None)
    assert await set_live(sm, enabled=False) == paused
    switch = LiveSwitch(sm)
    assert (await _refusal(switch)).kind == "paused"

    resumed = await set_live(sm, enabled=True)
    assert not resumed.paused
    assert await set_live(sm, enabled=True) == resumed
    await switch.check(Decimal(1))


async def test_a_provider_block_refuses_until_resumed(db_sessionmaker: Sessionmaker) -> None:
    sm = db_sessionmaker
    switch = LiveSwitch(sm)

    await switch.block_provider()
    assert (await _refusal(switch)).kind == "provider"
    blocked = await set_live(sm, enabled=False)
    assert blocked.provider_blocked_until is not None

    resumed = await set_live(sm, enabled=True)
    assert resumed.provider_blocked_until is None
    await switch.check(Decimal(1))


async def test_an_expired_provider_block_lets_the_next_call_probe(
    db_sessionmaker: Sessionmaker,
) -> None:
    switch = LiveSwitch(db_sessionmaker)
    await switch.block_provider(timedelta(seconds=-1))
    await switch.check(Decimal(1))


async def test_boot_inserts_a_missing_switch_and_keeps_an_existing_one(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    await _drop_switch(sm)
    await ensure_live_switch(sm)
    async with sm() as session:
        assert await session.scalar(select(func.count()).select_from(LiveControls)) == 1
    await set_live(sm, enabled=False)
    await ensure_live_switch(sm)
    assert (await _refusal(LiveSwitch(sm))).kind == "paused"


async def test_a_local_live_guard_reads_the_switch_before_the_ledger(
    tmp_path: Path, db_sessionmaker: Sessionmaker
) -> None:
    ledger = tmp_path / "spend_ledger.jsonl"
    ledger.write_text("")
    settings = settings_without_dotenv(model_mode="live", app_env="local", spend_ledger_path=ledger)
    guard = spend_guard(settings, sessionmaker=db_sessionmaker)
    assert isinstance(guard, BudgetGuard)
    key = CallKey(
        world_id=uuid4(),
        scenario="test",
        org="northlake",
        role="internal",
        thread=None,
        turn=1,
        turn_id=uuid4(),
        step_idx=0,
        attempt=1,
    )

    await set_live(db_sessionmaker, enabled=False)
    with pytest.raises(BudgetExhausted) as refused:
        await guard.reserve_amount(Decimal(2), key=key, model=OPUS_5_5)
    assert refused.value.kind == "paused"
    with SpendLedger(ledger).locked() as locked:
        assert locked.open_reservations() == {}

    await set_live(db_sessionmaker, enabled=True)
    reservation = await guard.reserve_amount(Decimal(2), key=key, model=OPUS_5_5)
    await guard.settle(
        reservation, cost_usd=Decimal("0.5"), usage={}, served_model=OPUS_5_5, stop_reason=None
    )
    assert SpendLedger(ledger).read().spent_usd == Decimal("0.5")
