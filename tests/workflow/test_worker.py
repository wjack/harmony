"""The worker process: queue wiring, graceful shutdown, signals, and the CLI."""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import timedelta
from uuid import uuid4

from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from typer.testing import CliRunner

from harmony.cli import app
from harmony.config import Settings
from harmony.temporal.policies import MAIN_TASK_QUEUE, MODEL_TASK_QUEUE
from harmony.temporal.worker import WorkerConfig, WorkerDeps, build_workers, serve, stop_on_signals
from harmony.temporal.workflows.hello import HelloWorkflow
from harmony.temporal.workflows.retire_world import RetireWorldWorkflow
from tests.workflow._support import Sessionmaker


async def test_the_main_queue_runs_the_p0_workflows(
    temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker, app_settings: Settings
) -> None:
    workers = build_workers(WorkerDeps(app_settings, temporal_env.client, db_sessionmaker))
    assert workers[0].task_queue == MAIN_TASK_QUEUE
    assert {worker.task_queue for worker in workers} <= {MAIN_TASK_QUEUE, MODEL_TASK_QUEUE}
    main = workers[0].config()
    assert {HelloWorkflow, RetireWorldWorkflow} <= set(main.get("workflows", []))
    assert main.get("graceful_shutdown_timeout") == timedelta(seconds=90)
    assert main.get("max_concurrent_activities") == WorkerConfig().main_max_concurrent_activities


class _Slow:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finished = False

    @activity.defn
    async def slow(self) -> None:
        self.started.set()
        await asyncio.sleep(0.5)
        self.finished = True


@workflow.defn(sandboxed=False)
class _SlowWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity_method(
            _Slow.slow, start_to_close_timeout=timedelta(seconds=30)
        )


async def test_shutdown_lets_a_running_activity_finish(temporal_env: WorkflowEnvironment) -> None:
    client = temporal_env.client
    slow = _Slow()
    queue = f"q-{uuid4()}"
    worker = Worker(
        client,
        task_queue=queue,
        workflows=[_SlowWorkflow],
        activities=[slow.slow],
        graceful_shutdown_timeout=timedelta(seconds=10),
    )
    stop = asyncio.Event()
    serving = asyncio.create_task(serve([worker], stop))
    await client.start_workflow(_SlowWorkflow.run, id=f"slow-{uuid4()}", task_queue=queue)
    await asyncio.wait_for(slow.started.wait(), timeout=10)

    stop.set()
    await asyncio.wait_for(serving, timeout=20)

    assert slow.finished
    assert worker.is_shutdown


async def test_sigterm_requests_a_stop_and_a_second_signal_is_left_to_the_default() -> None:
    stop = asyncio.Event()
    stop_on_signals(stop)
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), timeout=5)
    finally:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
    assert signal.getsignal(signal.SIGINT) == signal.default_int_handler


def test_cli_exposes_worker_and_dev_hello() -> None:
    runner = CliRunner()
    worker_help = runner.invoke(app, ["worker", "--help"])
    assert worker_help.exit_code == 0, worker_help.output
    assert "--graceful-shutdown" in worker_help.output

    hello_help = runner.invoke(app, ["dev", "hello", "--help"])
    assert hello_help.exit_code == 0, hello_help.output
    assert "--world" in hello_help.output

    missing = runner.invoke(app, ["dev", "hello"])
    assert missing.exit_code != 0
