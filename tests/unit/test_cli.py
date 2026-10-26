from __future__ import annotations

import sys
import types

import pytest
import typer
from typer.testing import CliRunner

from harmony.cli import app

runner = CliRunner()


def _fake_module(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: typer.Typer) -> None:
    module = types.ModuleType(name)
    for attr, value in attrs.items():
        setattr(module, attr, value)
    monkeypatch.setitem(sys.modules, name, module)


def test_help_lists_every_command_without_importing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("harmony.cli_seed", "harmony.cli_spend", "harmony.cli_dev", "harmony.cli_llm"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for command in ("seed", "spend", "worker", "dev", "llm"):
        assert command in result.output
    assert "harmony.cli_seed" not in sys.modules


def test_a_lazy_command_receives_its_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seed_app = typer.Typer()

    @seed_app.command()
    def seed(scenario: str = typer.Option(...)) -> None:  # pyright: ignore[reportUnusedFunction]
        typer.echo(f"seeding {scenario}")

    _fake_module(monkeypatch, "harmony.cli_seed", app=seed_app)
    result = runner.invoke(app, ["seed", "--scenario", "harmony-demo"])
    assert result.exit_code == 0, result.output
    assert "seeding harmony-demo" in result.output

    help_result = runner.invoke(app, ["seed", "--help"])
    assert help_result.exit_code == 0
    assert "--scenario" in help_result.output


def test_a_lazy_group_keeps_its_subcommands(monkeypatch: pytest.MonkeyPatch) -> None:
    llm_app = typer.Typer()

    @llm_app.callback()
    def _group() -> None:  # pyright: ignore[reportUnusedFunction]
        pass

    @llm_app.command()
    def ping(live: bool = False) -> None:  # pyright: ignore[reportUnusedFunction]
        typer.echo(f"ping live={live}")

    _fake_module(monkeypatch, "harmony.cli_llm", app=llm_app)
    result = runner.invoke(app, ["llm", "ping", "--live"])
    assert result.exit_code == 0, result.output
    assert "ping live=True" in result.output


def test_a_failing_lazy_command_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    spend_app = typer.Typer()

    @spend_app.command()
    def spend() -> None:  # pyright: ignore[reportUnusedFunction]
        raise typer.Exit(code=3)

    _fake_module(monkeypatch, "harmony.cli_spend", app=spend_app)
    assert runner.invoke(app, ["spend"]).exit_code == 3


def test_a_missing_module_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "harmony.cli_seed", None)
    result = runner.invoke(app, ["seed"])
    assert result.exit_code == 2
    assert "not available yet" in result.output
