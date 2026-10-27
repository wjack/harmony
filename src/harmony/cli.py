"""The `harmony` command.

Subcommands live in their owning tasks' modules and load lazily: `harmony --help`
imports nothing but this file, and `harmony spend` never imports Temporal or the
Anthropic SDK. Each module exposes a `typer.Typer`; one with a single command and no
callback collapses into that command (`harmony seed --scenario ...`), so a module that
wants a group (`harmony llm ping`) gives its Typer a `@app.callback()`.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass

import typer
import typer.main


@dataclass(frozen=True, slots=True)
class _LazyCommand:
    name: str
    module: str
    attr: str
    help: str


_LAZY_COMMANDS = (
    _LazyCommand(
        "seed",
        "harmony.cli_seed",
        "app",
        "Load a scenario into a new world and print its ID and row counts.",
    ),
    _LazyCommand(
        "spend",
        "harmony.cli_spend",
        "app",
        "Report harmony's development spend from the ledger.",
    ),
    _LazyCommand("worker", "harmony.cli_dev", "worker_app", "Run the Temporal worker."),
    _LazyCommand("dev", "harmony.cli_dev", "dev_app", "Developer utilities, e.g. `dev hello`."),
    _LazyCommand(
        "llm",
        "harmony.cli_llm",
        "app",
        "Model client utilities. `llm ping --live` makes one paid call.",
    ),
    _LazyCommand(
        "run",
        "harmony.cli_run",
        "app",
        "Run a scenario's story headless in a fresh world and report each beat.",
    ),
    _LazyCommand(
        "storage",
        "harmony.cli_storage",
        "app",
        "Object storage utilities. `storage check` puts, gets and deletes a probe.",
    ),
    _LazyCommand(
        "live",
        "harmony.cli_live",
        "app",
        "The live-models switch through the database: `live pause` or `live resume`.",
    ),
)

app = typer.Typer(
    name="harmony",
    help="Harmony: the network of Claude representatives.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)


def _load(spec: _LazyCommand) -> typer.Typer:
    try:
        module = importlib.import_module(spec.module)
    except ModuleNotFoundError as exc:
        if exc.name != spec.module:
            raise
        typer.echo(f"harmony {spec.name}: {spec.module} is not available yet", err=True)
        raise typer.Exit(code=2) from exc
    target: object = getattr(module, spec.attr, None)
    if not isinstance(target, typer.Typer):
        raise TypeError(f"{spec.module}.{spec.attr} must be a typer.Typer")
    return target


def _register(spec: _LazyCommand) -> None:
    # The placeholder swallows every argument (including --help) and hands them to the
    # real command, which is only imported when this subcommand actually runs.
    @app.command(
        name=spec.name,
        help=spec.help,
        add_help_option=False,
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )
    def _run(ctx: typer.Context) -> None:  # pyright: ignore[reportUnusedFunction]
        command = typer.main.get_command(_load(spec))
        root = ctx.find_root().info_name or "harmony"
        result: object = command.main(
            args=list(ctx.args), prog_name=f"{root} {spec.name}", standalone_mode=False
        )
        if isinstance(result, int) and result != 0:
            raise typer.Exit(code=result)


for _spec in _LAZY_COMMANDS:
    _register(_spec)


def main() -> None:
    app()
