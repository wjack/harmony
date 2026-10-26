"""`harmony worker` and `harmony dev ...`.

`worker_app` has one command and no callback, so it collapses into `harmony worker`;
`dev_app` has a callback, so its commands stay subcommands (`harmony dev hello`).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Annotated
from uuid import UUID, uuid4

import typer

from harmony.config import get_settings
from harmony.db.engine import create_sessionmaker, engine_from_settings
from harmony.errors import WorldRetired
from harmony.temporal.client import connect_client
from harmony.temporal.start import WorldNotFound, start_world_workflow, world_workflow_id
from harmony.temporal.worker import WorkerConfig, run_worker
from harmony.temporal.workflows.hello import HelloInput, HelloWorkflow

worker_app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)
dev_app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


@worker_app.command()
def worker(
    graceful_shutdown: Annotated[
        float,
        typer.Option(
            "--graceful-shutdown",
            min=0.0,
            help="Seconds running activities get to finish after SIGTERM or SIGINT.",
        ),
    ] = 90.0,
) -> None:
    """Run the Temporal worker for harmony-main and harmony-model."""
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    config = WorkerConfig(graceful_shutdown=timedelta(seconds=graceful_shutdown))
    asyncio.run(run_worker(get_settings(), config))


@dev_app.callback()
def dev() -> None:
    """Developer utilities."""


@dev_app.command()
def hello(
    world: Annotated[UUID, typer.Option("--world", help="A live world's ID.")],
    ticks: Annotated[int, typer.Option(min=1, max=100, help="How many hello.tick events.")] = 3,
    interval: Annotated[float, typer.Option(min=0.0, help="Seconds between ticks.")] = 1.0,
    wait: Annotated[float, typer.Option(min=1.0, help="Seconds to wait for the result.")] = 60.0,
) -> None:
    """Run HelloWorkflow in a world: hello.tick events through the outbox."""
    asyncio.run(_hello(world, ticks, timedelta(seconds=interval), wait))


async def _hello(world_id: UUID, ticks: int, interval: timedelta, wait_s: float) -> None:
    settings = get_settings()
    client = await connect_client(settings)
    engine = engine_from_settings(settings, application_name="harmony-cli")
    try:
        handle = await start_world_workflow(
            client,
            create_sessionmaker(engine),
            HelloWorkflow.run,
            HelloInput(world_id=world_id, ticks=ticks, interval=interval),
            world_id=world_id,
            workflow_id=world_workflow_id(world_id, "hello", uuid4().hex[:12]),
        )
    except (WorldNotFound, WorldRetired) as err:
        typer.echo(f"harmony dev hello: {err}", err=True)
        raise typer.Exit(code=1) from err
    finally:
        await engine.dispose()

    typer.echo(f"started {handle.id}")
    try:
        async with asyncio.timeout(wait_s):
            result = await handle.result()
    except TimeoutError as err:
        typer.echo(
            f"harmony dev hello: no result after {wait_s:g}s; is `harmony worker` running?",
            err=True,
        )
        raise typer.Exit(code=1) from err
    typer.echo(f"hello.tick seqs: {', '.join(str(seq) for seq in result.seqs)}")
