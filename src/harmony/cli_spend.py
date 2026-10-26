"""`harmony spend`: harmony's development spend, from the shared ledger (§14).

Read-only unless `--ack`: it takes a shared lock, never creates the file, and skips
every line that isn't harmony's. The first line of output is always
`harmony $<spent> of $<cap>`. Alerts nobody has acknowledged yet are printed as
`NEW ALERT ...` lines; `--ack` appends one `ack` line covering them (cost $0), which
is the only write this command makes. Exit code 2 means the ledger is missing,
unreadable or corrupt.

Imports nothing heavier than settings and the ledger module (no SDK, no database).
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from harmony.config import get_settings
from harmony.llm.errors import LedgerUnavailable
from harmony.llm.ledger import AlertRecord, LedgerEntry, LedgerSummary, SpendLedger

app = typer.Typer(add_completion=False, help="Report harmony's development spend.")

_CENT = Decimal("0.01")


@app.command()
def spend(
    ledger: Annotated[
        Path | None,
        typer.Option(
            "--ledger",
            help="The spend ledger to read (default: SPEND_LEDGER_PATH).",
            dir_okay=False,
        ),
    ] = None,
    new_alerts: Annotated[
        bool,
        typer.Option("--new-alerts", help="Print only the alerts not yet acknowledged."),
    ] = False,
    ack: Annotated[
        bool,
        typer.Option("--ack", help="Acknowledge the new alerts (appends one ack line)."),
    ] = False,
) -> None:
    """Report harmony's development spend from the ledger."""
    settings = get_settings()
    path = ledger or settings.spend_ledger_path
    if path is None:
        _fail("SPEND_LEDGER_PATH is not set; pass --ledger PATH")
    spend_ledger = SpendLedger(path.expanduser().absolute(), project=settings.spend_project)
    try:
        summary = spend_ledger.read()
        if not new_alerts:
            for line in report_lines(summary, settings.dev_budget_usd):
                typer.echo(line)
        for alert in summary.unacked:
            typer.echo(alert_line(alert))
        if ack:
            acked = acknowledge(spend_ledger, env=settings.app_env)
            typer.echo(
                "acknowledged " + ", ".join(_usd(t) for t in acked)
                if acked
                else "no new alerts to acknowledge"
            )
    except LedgerUnavailable as exc:
        _fail(str(exc))


def report_lines(summary: LedgerSummary, cap_usd: Decimal) -> list[str]:
    # Spend is rounded up and the remainder down, so the report never flatters.
    spent = summary.spent_usd.quantize(_CENT, rounding=ROUND_CEILING)
    remaining = max(Decimal(0), cap_usd - summary.spent_usd).quantize(_CENT, rounding=ROUND_FLOOR)
    alerts = ", ".join(_usd(a.threshold_usd) for a in summary.alerts) or "none"
    ignored = f"ignored {summary.foreign_lines:,} non-{summary.project} lines"
    if summary.foreign_malformed:
        ignored += f" and {summary.foreign_malformed:,} malformed lines"
    return [
        f"{summary.project} ${spent:,.2f} of ${cap_usd:,.2f}",
        f"remaining ${remaining:,.2f} · {summary.calls:,} calls · alerts written: {alerts}",
        ignored,
    ]


def alert_line(alert: AlertRecord) -> str:
    what = "cap reached (a call was refused)" if alert.reason == "refused" else "crossed"
    return (
        f"NEW ALERT {_usd(alert.threshold_usd)} {what} at {alert.ts} "
        f"(${alert.spent_usd:,.2f} spent)"
    )


def acknowledge(ledger: SpendLedger, *, env: str) -> tuple[Decimal, ...]:
    """Append one ack line for every alert not yet acknowledged; return their thresholds."""
    with ledger.locked() as locked:
        thresholds = tuple(sorted({a.threshold_usd for a in locked.summary().unacked}))
        if thresholds:
            locked.append(
                [
                    LedgerEntry(
                        project=ledger.project, kind="ack", env=env, acked_thresholds=thresholds
                    )
                ]
            )
    return thresholds


def _usd(amount: Decimal) -> str:
    return f"${amount:,.2f}"


def _fail(message: str) -> NoReturn:
    typer.echo(f"harmony spend: {message}", err=True)
    raise typer.Exit(code=2)


__all__ = ["acknowledge", "alert_line", "app", "report_lines"]
