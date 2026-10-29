"""`harmony llm ping` (dry run only; `--live` is never run in tests) and the budget
threshold the request builders pin (LIVE_DEMO_PLAN 3.1, 3.4)."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from harmony import cli_llm
from harmony.llm.contract import check_request
from harmony.llm.pricing import HAIKU_5_5, OPUS_5_5
from harmony.llm.requests import BETA_DISPLAY_UPDATES, BETA_FALLBACK_DEFAULT
from harmony.llm.wire import as_object, objects


def test_the_opus_ping_is_a_streamed_featured_internal_turn() -> None:
    request = cli_llm.ping_request("opus")
    body = request.body

    assert check_request(request) == []
    assert (body["model"], request.key.role) == (OPUS_5_5, "internal")
    assert as_object(body["thinking"]) == {"type": "adaptive", "display": "updates"}
    assert set(request.betas) == {BETA_FALLBACK_DEFAULT, BETA_DISPLAY_UPDATES}
    [tool] = objects(body["tools"])
    assert tool.get("type") is None
    assert tool["eager_input_streaming"] is True


def test_the_haiku_featured_ping_is_that_turn_on_haiku() -> None:
    request = cli_llm.ping_request("haiku-featured")
    body = request.body

    assert check_request(request) == []
    assert (body["model"], request.key.role) == (HAIKU_5_5, "internal")
    assert as_object(body["thinking"]) == {"type": "adaptive"}
    [tool] = objects(body["tools"])
    assert tool["eager_input_streaming"] is True


def test_the_haiku_ping_is_the_screener_shape() -> None:
    request = cli_llm.ping_request("haiku")
    body = request.body

    assert check_request(request) == []
    assert (body["model"], request.key.role) == (HAIKU_5_5, "screener")
    assert as_object(body["thinking"]) == {"type": "disabled"}
    output_config = as_object(body["output_config"]) or {}
    assert as_object(output_config["format"]) == {
        "type": "json_schema",
        "schema": cli_llm.PING_OUTPUT_SCHEMA,
    }
    assert request.betas == ()
    assert "tools" not in body


@pytest.mark.parametrize(
    ("model", "served"),
    [("opus", OPUS_5_5), ("haiku-featured", HAIKU_5_5), ("haiku", HAIKU_5_5)],
)
def test_the_dry_run_prints_the_body_and_sends_nothing(
    model: cli_llm.PingModel, served: str
) -> None:
    result = CliRunner().invoke(cli_llm.app, ["ping", "--model", model])
    assert result.exit_code == 0, result.output
    body_line, betas_line, summary = result.output.splitlines()
    request = cli_llm.ping_request(model)
    sent = json.loads(body_line)
    assert (sent["model"], sent["max_tokens"]) == (served, cli_llm.PING_MAX_TOKENS)
    assert betas_line == f"betas: {', '.join(request.betas) or 'none'}"
    assert summary.startswith("dry run: would reserve up to $")
    assert summary.endswith(f"on {served}; --live sends it")


def test_the_ping_needs_a_known_model() -> None:
    assert CliRunner().invoke(cli_llm.app, ["ping"]).exit_code == 2
    assert CliRunner().invoke(cli_llm.app, ["ping", "--model", "sonnet"]).exit_code == 2


def test_the_dry_run_never_loads_the_sdk() -> None:
    # In a clean interpreter, so no other test's imports count.
    probe = (
        "import sys\n"
        "from typer.testing import CliRunner\n"
        "from harmony import cli_llm\n"
        "for model in ('opus', 'haiku'):\n"
        "    assert CliRunner().invoke(cli_llm.app, ['ping', '--model', model]).exit_code == 0\n"
        "assert 'anthropic' not in sys.modules, 'dry run imported the SDK'\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, timeout=60)
