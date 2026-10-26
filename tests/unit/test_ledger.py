from __future__ import annotations

import json
import os
from collections import Counter
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

import harmony.cli_spend as cli_spend
from harmony.llm import CallKey
from harmony.llm.budget import BudgetGuard
from harmony.llm.errors import BudgetExhausted, LedgerUnavailable
from harmony.llm.ledger import LedgerEntry, SpendLedger
from harmony.llm.pricing import OPUS_5_5
from tests.conftest import settings_without_dotenv

REAL_LEDGER = Path("/Users/willjack/Projects/ant-project/spend_ledger.jsonl")
THRESHOLDS = (Decimal(50), Decimal(100), Decimal(200))

COUNTERBENCH_LINES = [
    {"run": "smoke", "model": "claude-opus-5-5", "cost_usd": 0.06, "ts": "2026-10-21T06:07:42Z"},
    {"run": "judge", "model": "claude-haiku-5-5", "cost_usd": 0.00068, "ts": "2026-10-21T06:08Z"},
    {"run": "smoke", "model": "claude-opus-5-5", "cost_usd": 1.5, "ts": "2026-10-21T06:09:00Z"},
]

runner = CliRunner()


def make_ledger(tmp_path: Path, lines: list[dict[str, object]] | None = None) -> Path:
    path = tmp_path / "spend_ledger.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in (lines or [])))
    return path


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


def guard_for(path: Path, cap: Decimal = Decimal(200)) -> BudgetGuard:
    return BudgetGuard(ledger=SpendLedger(path), cap_usd=cap, thresholds=THRESHOLDS, env="test")


async def spend(guard: BudgetGuard, amount: Decimal) -> None:
    reservation = await guard.reserve_amount(amount, key=key(), model=OPUS_5_5)
    await guard.settle(
        reservation,
        cost_usd=amount,
        usage={"input_tokens": 10, "output_tokens": 5},
        served_model=OPUS_5_5,
        stop_reason="end_turn",
    )


def harmony_lines(path: Path) -> list[dict[str, object]]:
    lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [line for line in lines if line.get("project") == "harmony"]


def alert_counts(path: Path) -> Counter[float]:
    return Counter(
        float(str(line["threshold_usd"])) for line in harmony_lines(path) if line["kind"] == "alert"
    )


# --- The file is never created, rewritten or truncated ----------------------------------


def test_a_missing_ledger_is_never_created(tmp_path: Path) -> None:
    path = tmp_path / "missing.jsonl"
    ledger = SpendLedger(path)
    with pytest.raises(LedgerUnavailable):
        ledger.check_usable()
    with pytest.raises(LedgerUnavailable):
        ledger.read()
    with pytest.raises(LedgerUnavailable), ledger.locked():
        pass
    assert not path.exists()


def test_a_directory_or_read_only_file_is_unusable(tmp_path: Path) -> None:
    with pytest.raises(LedgerUnavailable):
        SpendLedger(tmp_path).check_usable()
    path = make_ledger(tmp_path)
    path.chmod(0o444)
    try:
        if os.access(path, os.W_OK):  # running as root
            pytest.skip("root can write read-only files")
        with pytest.raises(LedgerUnavailable):
            SpendLedger(path).check_usable()
    finally:
        path.chmod(0o644)


def test_appends_keep_every_existing_byte(tmp_path: Path) -> None:
    path = make_ledger(tmp_path, COUNTERBENCH_LINES)
    before = path.read_bytes()
    ledger = SpendLedger(path)
    with ledger.locked() as locked:
        locked.append([LedgerEntry(project="harmony", kind="call", env="test")])
    after = path.read_bytes()
    assert after.startswith(before)
    assert after.count(b"\n") == before.count(b"\n") + 1


def test_a_foreign_line_cut_off_mid_write_is_terminated_before_ours(tmp_path: Path) -> None:
    path = tmp_path / "spend_ledger.jsonl"
    path.write_text('{"run": "smoke", "cost_usd": 0.5}\n{"run": "smo')
    ledger = SpendLedger(path)
    assert ledger.read().foreign_lines == 1  # the partial line isn't read yet
    with ledger.locked() as locked:
        locked.append([LedgerEntry(project="harmony", kind="call", env="test")])
    lines = path.read_text().splitlines()
    assert lines[1] == '{"run": "smo'
    assert json.loads(lines[2])["project"] == "harmony"
    summary = SpendLedger(path).read()
    assert (summary.calls, summary.foreign_lines, summary.foreign_malformed) == (1, 1, 1)


def test_a_partial_last_line_is_read_once_complete(tmp_path: Path) -> None:
    path = tmp_path / "spend_ledger.jsonl"
    line = LedgerEntry(
        project="harmony", kind="call", env="test", cost_usd=Decimal("1.5")
    ).to_line()
    path.write_bytes(line[:20])
    ledger = SpendLedger(path)
    assert ledger.read().spent_usd == 0
    with path.open("ab") as f:
        f.write(line[20:])
    assert ledger.read().spent_usd == Decimal("1.5")


def test_a_ledger_that_shrank_or_was_replaced_fails_closed(tmp_path: Path) -> None:
    path = make_ledger(tmp_path, COUNTERBENCH_LINES)
    ledger = SpendLedger(path)
    ledger.read()
    with path.open("r+b") as f:
        f.truncate(10)
    with pytest.raises(LedgerUnavailable, match="shrank"):
        ledger.read()

    other = tmp_path / "other.jsonl"
    other.write_bytes(b"")
    replaced = SpendLedger(other)
    replaced.read()
    copy = tmp_path / "copy.jsonl"
    copy.write_bytes(b"")
    os.replace(copy, other)  # same path, new file
    with pytest.raises(LedgerUnavailable, match="replaced"):
        replaced.read()


def test_malformed_lines(tmp_path: Path) -> None:
    path = make_ledger(tmp_path, COUNTERBENCH_LINES)
    with path.open("a") as f:
        f.write("not json at all\n")
        f.write("[1, 2]\n")
        f.write("\n")
    summary = SpendLedger(path).read()
    assert (summary.foreign_lines, summary.foreign_malformed, summary.spent_usd) == (4, 1, 0)

    with path.open("a") as f:
        f.write('{"project": "harmony", "cost_usd": 1.0, "kind": "call"\n')
    with pytest.raises(LedgerUnavailable, match="malformed harmony line"):
        SpendLedger(path).read()


def test_a_harmony_line_without_a_valid_cost_fails_closed(tmp_path: Path) -> None:
    for bad in ('"1.0"', "-1.0", "null", "true"):
        path = tmp_path / f"ledger-{len(bad)}-{bad[0]}.jsonl"
        path.write_text(f'{{"project": "harmony", "kind": "call", "cost_usd": {bad}}}\n')
        with pytest.raises(LedgerUnavailable, match="cost_usd"):
            SpendLedger(path).read()


# --- Lines other readers can sum ---------------------------------------------------------


async def test_a_counterbench_reader_sums_harmony_call_alert_and_ack_lines(tmp_path: Path) -> None:
    path = make_ledger(tmp_path, COUNTERBENCH_LINES)
    guard = guard_for(path)
    for _ in range(4):
        await spend(guard, Decimal("13.123456"))  # crosses $50 on the fourth
    acked = cli_spend.acknowledge(SpendLedger(path), env="test")
    assert acked == (Decimal(50),)

    kinds = Counter(str(line["kind"]) for line in harmony_lines(path))
    assert kinds == Counter({"call": 4, "alert": 1, "ack": 1})

    # Exactly how counterbench reads the shared file.
    with path.open() as f:
        total = sum(json.loads(line)["cost_usd"] for line in f)
    counterbench = sum(float(str(line["cost_usd"])) for line in COUNTERBENCH_LINES)
    assert total == pytest.approx(counterbench + 4 * 13.123456)

    for line in harmony_lines(path):
        assert isinstance(line["cost_usd"], float)
        if line["kind"] != "call":
            assert line["cost_usd"] == 0.0
    assert SpendLedger(path).read().spent_usd == Decimal("52.493824")


async def test_call_lines_carry_the_documented_fields(tmp_path: Path) -> None:
    path = make_ledger(tmp_path)
    guard = guard_for(path)
    call_key = key()
    reservation = await guard.reserve_amount(Decimal(1), key=call_key, model=OPUS_5_5)
    await guard.settle(
        reservation,
        cost_usd=Decimal("0.0215"),
        usage={
            "input_tokens": 1_000,
            "output_tokens": 500,
            "cache_read_input_tokens": 200,
            "cache_creation_input_tokens": 30,
            "server_tool_use": {"web_search_requests": 2},
        },
        served_model="claude-opus-5",
        stop_reason="end_turn",
    )
    [line] = harmony_lines(path)
    assert line["kind"] == "call"
    assert line["env"] == "test"
    assert line["world_id"] == str(call_key.world_id)
    assert line["turn_id"] == str(call_key.turn_id)
    assert (line["role"], line["step_idx"], line["attempt"]) == ("internal", 0, 1)
    assert (line["model_requested"], line["model_served"]) == (OPUS_5_5, "claude-opus-5")
    assert (line["input_tokens"], line["output_tokens"]) == (1_000, 500)
    assert (line["cache_read_input_tokens"], line["cache_creation_input_tokens"]) == (200, 30)
    assert line["web_searches"] == 2
    assert line["cost_usd"] == 0.0215
    assert "ts" in line
    assert "partial" not in line


# --- Alerts -------------------------------------------------------------------------------


async def test_each_threshold_alert_is_written_exactly_once(tmp_path: Path) -> None:
    path = make_ledger(tmp_path, COUNTERBENCH_LINES)
    guard = guard_for(path)
    for _ in range(66):
        await spend(guard, Decimal(3))
    assert alert_counts(path) == Counter({50.0: 1, 100.0: 1})

    # Spend can no longer reach $200 exactly, so the first refusal writes the cap alert.
    for _ in range(3):
        with pytest.raises(BudgetExhausted):
            await spend(guard, Decimal(3))
    assert alert_counts(path) == Counter({50.0: 1, 100.0: 1, 200.0: 1})
    refused = [line for line in harmony_lines(path) if line.get("threshold_usd") == 200.0]
    assert refused[0]["reason"] == "refused"

    # A fresh process reading the same ledger doesn't repeat them either.
    again = guard_for(path)
    with pytest.raises(BudgetExhausted):
        await spend(again, Decimal(3))
    assert alert_counts(path) == Counter({50.0: 1, 100.0: 1, 200.0: 1})
    assert SpendLedger(path).read().spent_usd == Decimal(198)


async def test_one_call_crossing_several_thresholds_writes_each_once(tmp_path: Path) -> None:
    path = make_ledger(tmp_path)
    guard = guard_for(path)
    await spend(guard, Decimal(200))
    assert alert_counts(path) == Counter({50.0: 1, 100.0: 1, 200.0: 1})
    reasons = {line["reason"] for line in harmony_lines(path) if line["kind"] == "alert"}
    assert reasons == {"crossed"}
    with pytest.raises(BudgetExhausted):
        await spend(guard, Decimal("0.01"))
    assert alert_counts(path) == Counter({50.0: 1, 100.0: 1, 200.0: 1})


# --- harmony spend ---------------------------------------------------------------------


@pytest.fixture
def cli_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_without_dotenv(app_env="local", spend_ledger_path=None)
    monkeypatch.setattr(cli_spend, "get_settings", lambda: settings)


@pytest.mark.usefixtures("cli_settings")
async def test_spend_reports_alerts_until_acknowledged(tmp_path: Path) -> None:
    path = make_ledger(tmp_path, COUNTERBENCH_LINES)
    guard = guard_for(path)
    for _ in range(34):
        await spend(guard, Decimal(3))  # $102: crosses $50 and $100

    result = runner.invoke(cli_spend.app, ["--ledger", str(path)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "harmony $102.00 of $200.00"
    assert lines[1].startswith("remaining $98.00 · 34 calls")
    assert lines[2] == "ignored 3 non-harmony lines"
    assert [line.split(" at ")[0] for line in lines[3:]] == [
        "NEW ALERT $50.00 crossed",
        "NEW ALERT $100.00 crossed",
    ]

    only_new = runner.invoke(cli_spend.app, ["--ledger", str(path), "--new-alerts"])
    assert only_new.exit_code == 0
    assert len(only_new.output.splitlines()) == 2

    size = path.stat().st_size
    acked = runner.invoke(cli_spend.app, ["--ledger", str(path), "--new-alerts", "--ack"])
    assert acked.exit_code == 0, acked.output
    assert acked.output.splitlines()[-1] == "acknowledged $50.00, $100.00"
    assert path.stat().st_size > size

    after = runner.invoke(cli_spend.app, ["--ledger", str(path), "--new-alerts"])
    assert after.output == ""
    again = runner.invoke(cli_spend.app, ["--ledger", str(path), "--ack"])
    assert again.output.splitlines()[-1] == "no new alerts to acknowledge"
    assert alert_counts(path) == Counter({50.0: 1, 100.0: 1})


@pytest.mark.usefixtures("cli_settings")
def test_spend_never_writes_without_ack(tmp_path: Path) -> None:
    path = make_ledger(tmp_path, COUNTERBENCH_LINES)
    path.chmod(0o444)  # a read-only ledger is still reportable
    try:
        before = path.stat()
        result = runner.invoke(cli_spend.app, ["--ledger", str(path)])
        assert result.exit_code == 0, result.output
        assert result.output.splitlines()[0] == "harmony $0.00 of $200.00"
        after = path.stat()
        assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    finally:
        path.chmod(0o644)


@pytest.mark.usefixtures("cli_settings")
def test_spend_fails_closed_without_a_ledger(tmp_path: Path) -> None:
    missing = tmp_path / "missing.jsonl"
    result = runner.invoke(cli_spend.app, ["--ledger", str(missing)])
    assert result.exit_code == 2
    assert not missing.exists()
    unset = runner.invoke(cli_spend.app, [])
    assert unset.exit_code == 2


@pytest.mark.usefixtures("cli_settings")
@pytest.mark.skipif(not REAL_LEDGER.is_file(), reason="the project's real ledger is not here")
def test_spend_reads_the_real_ledger_without_touching_it() -> None:
    # Read-only: the shared lock and O_RDONLY; the file's bytes and mtime are unchanged.
    before = REAL_LEDGER.stat()
    lines = [line for line in REAL_LEDGER.read_bytes().splitlines() if line.strip()]
    foreign = sum(1 for line in lines if b'"project": "harmony"' not in line)
    harmony = len(lines) - foreign

    result = runner.invoke(cli_spend.app, ["--ledger", str(REAL_LEDGER)])
    assert result.exit_code == 0, result.output
    output = result.output.splitlines()
    if harmony == 0:
        assert output[0] == "harmony $0.00 of $200.00"
    assert output[2] == f"ignored {foreign:,} non-harmony lines"
    after = REAL_LEDGER.stat()
    assert (after.st_size, after.st_mtime_ns, after.st_ino) == (
        before.st_size,
        before.st_mtime_ns,
        before.st_ino,
    )


def test_spend_imports_no_sdk_models_or_temporal(tmp_path: Path) -> None:
    import subprocess
    import sys

    path = make_ledger(tmp_path, COUNTERBENCH_LINES)
    probe = (
        "import sys\n"
        "from typer.testing import CliRunner\n"
        "from harmony import cli_spend\n"
        f"result = CliRunner().invoke(cli_spend.app, ['--ledger', {str(path)!r}])\n"
        "assert result.exit_code == 0, result.output\n"
        "heavy = {'anthropic', 'temporalio', 'harmony.db.models', 'harmony.llm.budget'}\n"
        "loaded = heavy & set(sys.modules)\n"
        "assert not loaded, loaded\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, timeout=60)
