"""`harmony live pause|resume`: the live-models switch (LIVE_DEMO_PLAN 3.4), written
straight to the database for when the API itself is down.

Run it in Render Shell on the worker or the web service, whichever is up: the image has
no psql and Postgres takes no outside connections, and this reuses the app's own
settings, so the database URL never goes on a command line. It does what `POST
/api/admin/live` does (`budget_pg.set_live`): an upsert of the switch row, and on resume
a cleared provider block. No cap is touched.
"""

from __future__ import annotations

import asyncio

import typer

from harmony.config import get_settings
from harmony.db.engine import create_sessionmaker, engine_from_settings
from harmony.llm.budget_pg import LiveReport, set_live

app = typer.Typer(add_completion=False, help="The live-models switch, through the database.")


@app.callback()
def live() -> None:
    """The live-models switch, through the database."""


@app.command()
def pause() -> None:
    """Refuse every live model call from now on."""
    _report(asyncio.run(_set(enabled=False)))


@app.command()
def resume() -> None:
    """Allow live model calls again, clearing a provider block."""
    _report(asyncio.run(_set(enabled=True)))


async def _set(*, enabled: bool) -> LiveReport:
    engine = engine_from_settings(get_settings(), application_name="harmony-live")
    try:
        return await set_live(create_sessionmaker(engine), enabled=enabled)
    finally:
        await engine.dispose()


def _report(report: LiveReport) -> None:
    typer.echo(f"live models {'paused' if report.paused else 'on'}")
    if report.provider_blocked_until is not None:
        typer.echo(f"provider blocked until {report.provider_blocked_until.isoformat()}")
    if report.global_ is not None:
        typer.echo(f"demo spend ${report.global_.spent_usd:.2f} of ${report.global_.cap_usd:.2f}")
