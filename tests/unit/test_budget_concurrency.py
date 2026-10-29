from __future__ import annotations

import asyncio
import json
import random
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from harmony.llm import CallKey
from harmony.llm.budget import BudgetGuard, FreeSpend, Reservation, spend_guard
from harmony.llm.errors import BudgetExhausted, LedgerUnavailable
from harmony.llm.ledger import SpendLedger
from harmony.llm.pricing import OPUS_5_5
from tests.conftest import settings_without_dotenv

CAP = Decimal(200)
THRESHOLDS = (Decimal(50), Decimal(100), Decimal(200))


def key() -> CallKey:
    return CallKey(
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


def empty_ledger(tmp_path: Path) -> Path:
    path = tmp_path / "spend_ledger.jsonl"
    path.write_text('{"run": "smoke", "model": "claude-opus-5-5", "cost_usd": 0.06}\n')
    return path


def guard_for(path: Path) -> BudgetGuard:
    return BudgetGuard(ledger=SpendLedger(path), cap_usd=CAP, thresholds=THRESHOLDS, env="test")


def alert_counts(path: Path) -> Counter[float]:
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    return Counter(
        line["threshold_usd"]
        for line in lines
        if line.get("project") == "harmony" and line["kind"] == "alert"
    )


# --- The dev cap ------------------------------------------------------------------------


async def test_100_concurrent_3_dollar_calls_never_exceed_200(tmp_path: Path) -> None:
    path = empty_ledger(tmp_path)
    guard = guard_for(path)
    peaks: list[Decimal] = []
    outcomes: list[str] = []
    settled = Decimal(0)
    rng = random.Random(6)

    async def call() -> None:
        nonlocal settled
        await asyncio.sleep(rng.random() / 100)
        try:
            reservation = await guard.reserve_amount(Decimal(3), key=key(), model=OPUS_5_5)
        except BudgetExhausted:
            outcomes.append("refused")
            return
        # What the cap is checked against: settled spend plus everything in flight.
        # (`settle` drops the reservation and returns with no await in between, so
        # this count and `in_flight_usd` never overlap.)
        peaks.append(settled + guard.in_flight_usd)
        await asyncio.sleep(rng.random() / 100)  # the "call"
        await guard.settle(
            reservation,
            cost_usd=Decimal(3),
            usage={"input_tokens": 100, "output_tokens": 100},
            served_model=OPUS_5_5,
            stop_reason="end_turn",
        )
        settled += 3
        outcomes.append("settled")

    await asyncio.gather(*(call() for _ in range(100)))

    assert max(peaks) <= CAP
    assert Counter(outcomes) == Counter({"settled": 66, "refused": 34})
    assert SpendLedger(path).read().spent_usd == Decimal(198)
    assert guard.in_flight_usd == 0
    assert alert_counts(path) == Counter({50.0: 1, 100.0: 1, 200.0: 1})
    with path.open() as f:
        assert sum(json.loads(line)["cost_usd"] for line in f) == pytest.approx(198.06)


async def test_reservations_held_at_once_are_bounded_by_the_cap(tmp_path: Path) -> None:
    path = empty_ledger(tmp_path)
    guard = guard_for(path)
    results = await asyncio.gather(
        *(guard.reserve_amount(Decimal(3), key=key(), model=OPUS_5_5) for _ in range(100)),
        return_exceptions=True,
    )
    held = [r for r in results if isinstance(r, Reservation)]
    refused = [r for r in results if isinstance(r, BudgetExhausted)]
    assert (len(held), len(refused)) == (66, 34)
    assert guard.in_flight_usd == Decimal(198)

    # Releasing (calls that billed nothing) frees the room again; double release is a no-op.
    for reservation in held:
        await guard.release(reservation)
        await guard.release(reservation)
    assert guard.in_flight_usd == 0
    assert SpendLedger(path).read().spent_usd == 0
    await guard.reserve_amount(Decimal(200), key=key(), model=OPUS_5_5)


async def test_settle_is_idempotent(tmp_path: Path) -> None:
    path = empty_ledger(tmp_path)
    guard = guard_for(path)
    reservation = await guard.reserve_amount(Decimal(3), key=key(), model=OPUS_5_5)
    for _ in range(2):
        await guard.settle(
            reservation,
            cost_usd=Decimal("2.5"),
            usage={},
            served_model=OPUS_5_5,
            stop_reason="end_turn",
        )
    assert SpendLedger(path).read().calls == 1


async def test_a_reservation_stays_counted_if_its_line_cannot_be_written(tmp_path: Path) -> None:
    path = empty_ledger(tmp_path)
    guard = guard_for(path)
    reservation = await guard.reserve_amount(Decimal(150), key=key(), model=OPUS_5_5)
    path.chmod(0o444)
    try:
        with pytest.raises(LedgerUnavailable):
            await guard.settle(
                reservation,
                cost_usd=Decimal(150),
                usage={},
                served_model=OPUS_5_5,
                stop_reason=None,
            )
    finally:
        path.chmod(0o644)
    assert guard.in_flight_usd == Decimal(150)
    with pytest.raises(BudgetExhausted):
        await guard.reserve_amount(Decimal(51), key=key(), model=OPUS_5_5)


async def test_guards_over_one_ledger_see_each_others_calls_in_flight(tmp_path: Path) -> None:
    """The dev worker and `harmony llm ping --live` each build their own guard."""
    path = empty_ledger(tmp_path)
    worker, ping = guard_for(path), guard_for(path)
    running = await worker.reserve_amount(Decimal(150), key=key(), model=OPUS_5_5)
    with pytest.raises(BudgetExhausted, match=r"\$150.00 in flight"):
        await ping.reserve_amount(Decimal(51), key=key(), model=OPUS_5_5)
    held = await ping.reserve_amount(Decimal(50), key=key(), model=OPUS_5_5)

    await worker.settle(
        running, cost_usd=Decimal(100), usage={}, served_model=OPUS_5_5, stop_reason="end_turn"
    )
    await ping.reserve_amount(Decimal(50), key=key(), model=OPUS_5_5)  # 100 + 50 + 50
    with pytest.raises(BudgetExhausted):
        await worker.reserve_amount(Decimal("0.01"), key=key(), model=OPUS_5_5)
    await ping.release(held)
    await worker.reserve_amount(Decimal(50), key=key(), model=OPUS_5_5)


HOLD_A_RESERVATION = """
import asyncio, sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from harmony.llm import CallKey
from harmony.llm.budget import BudgetGuard
from harmony.llm.ledger import SpendLedger

async def main() -> None:
    guard = BudgetGuard(
        ledger=SpendLedger(Path(sys.argv[1])), cap_usd=Decimal(200),
        thresholds=(Decimal(200),), env="test",
    )
    key = CallKey(world_id=uuid4(), scenario="test", org="northlake", role="internal",
                  thread=None, turn=1, turn_id=uuid4(), step_idx=0, attempt=1)
    await guard.reserve_amount(Decimal(150), key=key, model="claude-opus-5-5")
    print("reserved", flush=True)
    await asyncio.Event().wait()  # a call in flight, until the process is killed

asyncio.run(main())
"""


async def test_a_call_in_flight_in_another_process_counts_until_that_process_dies(
    tmp_path: Path,
) -> None:
    path = empty_ledger(tmp_path)
    other = await asyncio.create_subprocess_exec(
        sys.executable, "-c", HOLD_A_RESERVATION, str(path), stdout=asyncio.subprocess.PIPE
    )
    try:
        assert other.stdout is not None
        async with asyncio.timeout(30):
            assert await other.stdout.readline() == b"reserved\n"
        guard = guard_for(path)
        with pytest.raises(BudgetExhausted):
            await guard.reserve_amount(Decimal(51), key=key(), model=OPUS_5_5)
    finally:
        other.kill()
        await other.wait()
    # A process that died can never settle its reservation; it no longer counts.
    await guard.reserve_amount(Decimal(200), key=key(), model=OPUS_5_5)


async def test_a_guard_with_nothing_to_charge_refuses() -> None:
    guard = BudgetGuard(ledger=None, cap_usd=CAP, thresholds=THRESHOLDS, env="ci")
    with pytest.raises(BudgetExhausted) as uncovered:
        await guard.reserve_amount(Decimal(1), key=key(), model=OPUS_5_5)
    # Fail closed: a guard with neither a ledger nor the live switch reads as paused.
    assert uncovered.value.kind == "paused"


async def test_the_dev_cap_refuses_as_the_whole_demo(tmp_path: Path) -> None:
    guard = guard_for(empty_ledger(tmp_path))
    with pytest.raises(BudgetExhausted) as refused:
        await guard.reserve_amount(CAP + 1, key=key(), model=OPUS_5_5)
    assert (refused.value.kind, refused.value.budget) == ("global", "dev")


# --- Live mode refuses to start without its ledger ------------------------------------


def test_live_mode_with_a_missing_ledger_refuses_to_start(tmp_path: Path) -> None:
    missing = tmp_path / "spend_ledger.jsonl"
    settings = settings_without_dotenv(
        model_mode="live", app_env="local", spend_ledger_path=missing
    )
    with pytest.raises(LedgerUnavailable, match="never created"):
        spend_guard(settings)
    assert not missing.exists()

    unset = settings_without_dotenv(model_mode="live", app_env="local", spend_ledger_path=None)
    with pytest.raises(LedgerUnavailable, match="SPEND_LEDGER_PATH"):
        spend_guard(unset)


def test_live_mode_with_an_unusable_ledger_refuses_to_start(tmp_path: Path) -> None:
    as_directory = settings_without_dotenv(
        model_mode="live", app_env="local", spend_ledger_path=tmp_path
    )
    with pytest.raises(LedgerUnavailable):
        spend_guard(as_directory)

    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text('{"project": "harmony", "kind": "call", "cost_usd": \n')
    with pytest.raises(LedgerUnavailable):
        spend_guard(
            settings_without_dotenv(model_mode="live", app_env="local", spend_ledger_path=corrupt)
        )


def test_live_mode_with_its_ledger_starts_and_scripted_mode_is_free(tmp_path: Path) -> None:
    path = empty_ledger(tmp_path)
    live = spend_guard(
        settings_without_dotenv(model_mode="live", app_env="local", spend_ledger_path=path)
    )
    assert isinstance(live, BudgetGuard)
    scripted = spend_guard(settings_without_dotenv(model_mode="scripted"))
    assert isinstance(scripted, FreeSpend)
    assert path.read_text().count("\n") == 1
