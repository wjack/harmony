"""Check `harmony spend` and the budget alerts (a P0 exit criterion). No model calls.

1. **A temporary ledger.** Simulated calls settle through the BudgetGuard with this
   project's cap and thresholds (`reserve_amount` + `settle`; nothing is sent anywhere):
   - spend up to just under the cap: `harmony spend` reports each crossed threshold as
     a new alert, once;
   - a second guard, as after a restart, keeps spending: no alert is written again;
   - `--ack` acknowledges them, after which `--new-alerts` prints nothing;
   - spending past the cap is refused, and the cap's alert is written once however
     often that happens.
   The ledger ends with exactly one alert line per threshold.
2. **The real ledger** (SPEND_LEDGER_PATH, or `--real-ledger`): `harmony spend` reads it
   without changing a byte and reports harmony's spend against its dev budget. The
   amount isn't checked: live testing on the development key has spent from it.

Prints PASS or FAIL per check and exits 1 on any failure.

    uv run python scripts/p0_spend_check.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from harmony.config import Settings, get_settings
from harmony.llm import CallKey
from harmony.llm.budget import BudgetGuard
from harmony.llm.errors import BudgetExhausted
from harmony.llm.ledger import SpendLedger
from harmony.llm.pricing import OPUS_5_5

ENV = "p0-exit"


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" ({detail})" if detail and not ok else ""))
        self.failures += not ok
        return ok


def harmony_command() -> list[str]:
    # The console script next to this interpreter, so the check runs the installed CLI.
    script = Path(sys.executable).with_name("harmony")
    if script.is_file():
        return [str(script)]
    found = shutil.which("harmony")
    if found is None:
        raise SystemExit("p0_spend_check: the `harmony` command is not installed")
    return [found]


def harmony_spend(ledger: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*harmony_command(), "spend", "--ledger", str(ledger), *flags],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "MODEL_MODE": "scripted"},
    )


def new_alerts(ledger: Path) -> list[str]:
    result = harmony_spend(ledger, "--new-alerts")
    if result.returncode != 0:
        raise RuntimeError(f"harmony spend --new-alerts exited {result.returncode}")
    # "NEW ALERT $50.00 crossed at <ts> (...)": compare without the timestamp.
    return [line.split(" at ")[0] for line in result.stdout.splitlines()]


def alert_lines(ledger: Path, project: str) -> Counter[Decimal]:
    lines = [json.loads(raw) for raw in ledger.read_text().splitlines() if raw.strip()]
    return Counter(
        Decimal(str(line["threshold_usd"]))
        for line in lines
        if line.get("project") == project and line.get("kind") == "alert"
    )


def call_key() -> CallKey:
    return CallKey(
        world_id=uuid4(),
        scenario="p0-exit",
        org="northlake",
        role="internal",
        thread=None,
        turn=1,
        turn_id=uuid4(),
        step_idx=0,
        attempt=1,
    )


async def simulate_call(guard: BudgetGuard, amount: Decimal) -> None:
    reservation = await guard.reserve_amount(amount, key=call_key(), model=OPUS_5_5)
    await guard.settle(
        reservation,
        cost_usd=amount,
        usage={"input_tokens": 1, "output_tokens": 1},
        served_model=OPUS_5_5,
        stop_reason="end_turn",
    )


def label(threshold: Decimal, reason: str) -> str:
    return f"NEW ALERT ${threshold:,.2f} {reason}"


async def check_alerts(report: Report, settings: Settings, workdir: Path) -> None:
    ledger_path = workdir / "spend_ledger.jsonl"
    ledger_path.touch()  # the guard never creates a ledger
    ledger = SpendLedger(ledger_path, project=settings.spend_project)
    cap = settings.dev_budget_usd
    thresholds = sorted(settings.alert_thresholds)

    def guard() -> BudgetGuard:
        return BudgetGuard(ledger=ledger, cap_usd=cap, thresholds=thresholds, env=ENV)

    # Thirty-odd calls to just under the cap: every threshold below it is crossed.
    step = (cap / 31).quantize(Decimal("0.01"))
    first = guard()
    while ledger.read().spent_usd + 2 * step < cap:
        await simulate_call(first, step)
    crossed = [t for t in thresholds if t <= ledger.read().spent_usd]
    expected = [label(t, "crossed") for t in crossed]
    report.check(
        "harmony spend reports each crossed threshold once",
        bool(crossed) and new_alerts(ledger_path) == expected,
        f"expected {expected}",
    )
    report.check("a second run reports the same alerts", new_alerts(ledger_path) == expected)

    # A new process reading the same ledger writes nothing again.
    await simulate_call(guard(), step)
    report.check(
        "a restarted guard doesn't repeat an alert",
        alert_lines(ledger_path, settings.spend_project) == Counter(crossed),
    )

    acked = harmony_spend(ledger_path, "--new-alerts", "--ack")
    report.check("--ack acknowledges them", acked.returncode == 0)
    report.check("then no alert is new", new_alerts(ledger_path) == [])

    # Past the cap: refused every time, and the cap's alert is written once.
    refusals = 0
    for _ in range(3):
        try:
            await simulate_call(guard(), 2 * step)
        except BudgetExhausted:
            refusals += 1
    report.check("calls past the cap are refused", refusals == 3)
    cap_alerts = [t for t in thresholds if t >= cap and t not in crossed]
    report.check(
        "the cap's alert is new exactly once",
        new_alerts(ledger_path)
        == [label(t, "cap reached (a call was refused)") for t in cap_alerts],
    )
    written = alert_lines(ledger_path, settings.spend_project)
    report.check(
        "the ledger holds one alert line per threshold",
        written == Counter(crossed + cap_alerts),
        f"{dict(written)}",
    )


def check_real_ledger(report: Report, settings: Settings, path: Path | None) -> None:
    if path is None or not path.is_file():
        report.check("the real ledger exists", False, f"{path} (set SPEND_LEDGER_PATH)")
        return
    before = path.stat()
    result = harmony_spend(path)
    after = path.stat()
    first = result.stdout.splitlines()[0] if result.stdout else ""
    report.check("harmony spend reads the real ledger", result.returncode == 0)
    report.check(
        "the real ledger reports harmony's spend against its budget",
        re.fullmatch(
            rf"{re.escape(settings.spend_project)} \$[\d,]+\.\d\d of "
            rf"{re.escape(f'${settings.dev_budget_usd:,.2f}')}",
            first,
        )
        is not None,
        first,
    )
    report.check(
        "the real ledger is unchanged",
        (after.st_size, after.st_mtime_ns, after.st_ino)
        == (before.st_size, before.st_mtime_ns, before.st_ino),
    )


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Check harmony spend and the budget alerts.")
    parser.add_argument(
        "--real-ledger",
        type=Path,
        default=settings.spend_ledger_path,
        help="The shared ledger to read (default: SPEND_LEDGER_PATH).",
    )
    args = parser.parse_args()

    report = Report()
    with tempfile.TemporaryDirectory(prefix="harmony-p0-spend-") as workdir:
        asyncio.run(check_alerts(report, settings, Path(workdir)))
    check_real_ledger(report, settings, args.real_ledger)
    sys.exit(1 if report.failures else 0)


if __name__ == "__main__":
    main()
