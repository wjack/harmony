"""`harmony run` end to end on `ScriptedModel` (LIVE_DEMO_PLAN L3).

- `harmony run --message … --approve all` plays a free-form request headless: the
  command as typed, on the scripted model over tests/fixtures/cassettes, with its own
  in-process worker against a local Temporal dev server and this test's database. The
  cassettes key Alex's first chat, not its text, so any message he sends plays it, and
  approving every listed row brings in Calder, whose side is scripted too. `--edit` has
  Dana tell Ridgeline's Claude something once Ridgeline has asked its question: the
  cassettes write its guidance, and `guidance_followed` reports what Ridgeline sent next
  (EDIT.md §9). The report then prints every engine field (3.14, 5.2), and the world is
  retired.
- `in_process` against an app with `ACCESS_CODE` set unlocks the gate as a visitor
  would, then starts a session.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Final
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.testing import WorkflowEnvironment
from typer.testing import CliRunner

from harmony import cli_run
from harmony.api.app import create_app
from harmony.config import Settings
from harmony.db.models import World
from harmony.storyrun.api import BASE_URL, ApiError, HarmonyApi, in_process
from harmony.storyrun.report import RULE
from harmony.worlds.retirer_temporal import TemporalRetirer

type Sessionmaker = async_sessionmaker[AsyncSession]

SCENARIO: Final = "harmony-demo"
MESSAGE: Final = (
    "We need powder XRD and SEM/EDS on 12 air-sensitive cathode powders by Nov 6, "
    "and an airtight holder to get them there under argon."
)
# Every field the engine report prints, in order (cli_run._engine_report).
ENGINE_FIELDS: Final = (
    "models used:",
    "turn latency: p50",
    "refusals:",
    "threads stopped short:",
    "TurnIncomplete:",
    "BudgetExhausted:",
    "model retries:",
    "failed attempts:",
    "ledger spend this run: $",
    "world spend: $",
    "at each beat:",
    "lineage peak spent + reserved: $",
    "re-priced at production prices: $",
    "peak RSS (in-process worker, app and player):",
)
CHECKS: Final = (
    "early_closes:",
    "close_refusals:",
    "consults_after_send:",
    "closes_after_refused_consult:",
    "status_checks_after_send:",
    "waiting_closes:",
    "empty_turn_failures:",
    "empty_end_turns_handled:",
    "multi_send_turns:",
    "silent_turn_pairs:",
    "meta_notes:",
    "cross_mentions:",
    "opener_leaks (of ",
    "not_run_sends:",
    "missing_field_errors:",
    "titles:",
    "drafts:",
    "short_question over ",
    "routes_without_why:",
    "auto_sends:",
    "auto_send_breaches:",
    "auto_requests_per_turn (max):",
    "duplicate_auto_recipients:",
    "guidance_followed:",
)
EDIT: Final = "ridgeline:henderson:2:Let's not give the Kalrez option"
RUN_TIMEOUT_S: Final = 240
COUNTERPARTIES: Final = (
    "Northlake Characterization Core",
    "Ridgeline Fabrication",
    "Calder Computational",
)


def _run_settings(settings: Settings, temporal: WorkflowEnvironment, ledger: Path) -> Settings:
    """The test's database, its dev server and an empty ledger of its own; never an API
    key, so nothing can reach a hosted service."""
    return settings.model_copy(
        update={
            "model_mode": "scripted",
            "temporal_address": temporal.client.service_client.config.target_host,
            "temporal_namespace": temporal.client.namespace,
            "temporal_api_key": None,
            "spend_ledger_path": ledger,
        }
    )


def _section(output: str, title: str) -> list[str]:
    """A report section's lines, up to the next section's rule."""
    lines = output.splitlines()
    start = lines.index(title) + 2  # the title, then its rule
    end = next((i for i in range(start, len(lines)) if lines[i] == RULE), len(lines))
    return lines[start:end]


async def test_a_message_plays_headless_and_the_report_prints_every_field(
    app_settings: Settings,
    db_sessionmaker: Sessionmaker,
    temporal_env: WorkflowEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = tmp_path / "spend_ledger.jsonl"
    ledger.touch()
    settings = _run_settings(app_settings, temporal_env, ledger)
    monkeypatch.setattr(cli_run, "get_settings", lambda: settings)

    # `run` owns its event loop (asyncio.run), as it does from the shell.
    result = await asyncio.to_thread(
        CliRunner().invoke,
        cli_run.app,
        [
            SCENARIO,
            "--message",
            MESSAGE,
            "--approve",
            "all",
            "--edit",
            EDIT,
            "--timeout",
            str(RUN_TIMEOUT_S),
        ],
        env={"COLUMNS": "160"},
    )
    output = result.output
    assert result.exit_code == 0, output
    assert "story message as alex, model scripted, worker in-process" in output
    assert "story complete" in output

    engine = _section(output, "Engine")
    for field in ENGINE_FIELDS:
        assert sum(line.startswith("  " + field) for line in engine) == 1, (field, engine)
    assert not any("SETUP FAILURE" in line for line in engine), engine
    assert "  ledger spend this run: $0.0000" in engine
    checks = _section(output, "Engine checks")
    for label in CHECKS:
        assert sum(line.startswith("  " + label) for line in checks) == 1, (label, checks)
    # Henderson's openers to Northlake and Ridgeline; Calder has no patterns, and the
    # seeded requests from Ridgeline and Vantage went to Northlake alone.
    assert "  opener_leaks (of 2 checked): 0" in checks
    # The story never consults after a send (decision 1), so nothing was refused.
    assert "  consults_after_send: 0" in checks
    # Claude says it's out after Alex approves, and checks nothing (decision 7).
    assert "  status_checks_after_send: 0" in checks
    # Dana's Edit: guidance from story_suggested_threads.yaml, and Ridgeline's next
    # message, which doesn't mention it (whether it follows it is for a live run).
    followed = checks[checks.index("  guidance_followed: 1") + 1 :]
    assert followed[:3] == [
        "    - Ridgeline Fabrication with Henderson Lab: sent without mentioning it; read "
        "whether it follows it",
        "      instruction: Let's not give the Kalrez option",
        "      guidance: Don't propose the Kalrez seal option.",
    ]
    assert followed[3].startswith("      next message: ")

    # The chat's one request, followed to its end on every row the card listed. The
    # world's rows go with it once retired, so this reads the report.
    for responder in COUNTERPARTIES:
        state = _section(output, f"Thread: Henderson Lab and {responder}")[0]
        assert state.endswith("(closed)"), (responder, state)
    retired = re.search(r"^world ([0-9a-f-]{36}) retired$", output, re.MULTILINE)
    assert retired is not None, output
    async with db_sessionmaker() as session:
        world = await session.get_one(World, UUID(retired[1]))
    assert (world.status, world.retired_at is not None) == ("retired", True)


async def test_the_run_unlocks_a_gated_app_before_it_starts_a_session(
    app_settings: Settings,
    db_engine: AsyncEngine,
    db_sessionmaker: Sessionmaker,
    temporal_env: WorkflowEnvironment,
) -> None:
    settings = app_settings.model_copy(update={"access_code": SecretStr("correct horse battery")})
    client = temporal_env.client
    retirer = TemporalRetirer(client, db_sessionmaker)

    # The gate is on: a visitor who hasn't unlocked it can't start a session.
    app = create_app(settings, engine=db_engine, temporal=client, retirer=retirer)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        with pytest.raises(ApiError) as locked:
            await HarmonyApi(http).start_session(SCENARIO)
    assert locked.value.status in (401, 403), locked.value

    async with in_process(settings, engine=db_engine, temporal=client, retirer=retirer) as api:
        session = await api.start_session(SCENARIO)
        assert session.world.scenario == SCENARIO
