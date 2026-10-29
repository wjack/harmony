"""The worker process: queue wiring, capacity from settings, the stale-reservation
sweep, graceful shutdown, signals, and the CLI."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Sequence
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from typer.testing import CliRunner

from harmony import cli_dev
from harmony.cli import app
from harmony.config import Settings
from harmony.db.models import LiveControls
from harmony.storyrun.worker import running_worker
from harmony.temporal import worker as worker_module
from harmony.temporal.policies import (
    CHAT_TASK_QUEUE,
    MAIN_TASK_QUEUE,
    MODEL_TASK_QUEUE,
)
from harmony.temporal.worker import (
    WorkerConfig,
    WorkerDeps,
    build_workers,
    model_runtime,
    serve,
    stop_on_signals,
)
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


def _activity_names(worker: Worker) -> set[str]:
    return {
        getattr(fn, "__temporal_activity_definition").name
        for fn in worker.config().get("activities", [])
    }


async def test_each_model_queue_runs_its_own_activities(
    temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker, app_settings: Settings
) -> None:
    settings = app_settings.model_copy(update={"model_mode": "scripted"})
    runtime = model_runtime(settings, db_sessionmaker)
    try:
        workers = build_workers(WorkerDeps(settings, temporal_env.client, db_sessionmaker, runtime))
    finally:
        await runtime.publisher.aclose()
    by_queue = {worker.task_queue: worker for worker in workers}
    assert list(by_queue) == [MAIN_TASK_QUEUE, MODEL_TASK_QUEUE, CHAT_TASK_QUEUE]
    # A steer's model calls run beside the representative's and the answerer's.
    model = {"rep_turn", "answer_consult", "write_guidance", "rewrite_held"}
    assert _activity_names(by_queue[MODEL_TASK_QUEUE]) == model
    assert _activity_names(by_queue[CHAT_TASK_QUEUE]) == {"internal_turn"}
    assert not {*model, "internal_turn"} & _activity_names(by_queue[MAIN_TASK_QUEUE])


async def test_a_live_worker_blocks_the_provider_through_its_guard(
    db_sessionmaker: Sessionmaker, app_settings: Settings
) -> None:
    """A provider spend-limit refusal sets the guard's block (3.4), so later live calls
    are refused locally instead of reaching the API again."""
    from harmony.llm.anthropic_model import AnthropicModel

    settings = app_settings.model_copy(
        update={
            "model_mode": "live",
            "app_env": "ci",
            "anthropic_api_key": SecretStr("test-key-not-real"),
            "spend_ledger_path": None,
        }
    )
    runtime = model_runtime(settings, db_sessionmaker)
    try:
        assert isinstance(runtime.client, AnthropicModel)
        assert runtime.client._on_provider_limit == runtime.guard.block_provider
    finally:
        await runtime.publisher.aclose()


async def test_harmony_worker_takes_its_capacity_from_settings(
    temporal_env: WorkflowEnvironment,
    app_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = app_settings.model_copy(
        update={
            "model_mode": "scripted",
            "worker_model_concurrency": 12,
            "worker_model_rate": 5.0,
            "worker_chat_concurrency": 4,
        }
    )
    # The CLI hands `run_worker` the settings and the shutdown grace, nothing else.
    handed: list[tuple[Settings, timedelta]] = []

    async def fake_run_worker(given: Settings, *, graceful_shutdown: timedelta) -> None:
        handed.append((given, graceful_shutdown))

    monkeypatch.setattr(cli_dev, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_dev, "run_worker", fake_run_worker)
    monkeypatch.setattr(cli_dev, "configure_logging", lambda: None)
    result = await asyncio.to_thread(
        CliRunner().invoke, app, ["worker", "--graceful-shutdown", "5"]
    )
    assert result.exit_code == 0, result.output
    assert handed == [(settings, timedelta(seconds=5))]

    # `run_worker` builds its workers from those settings, after the live switch exists.
    calls: list[str] = []
    served: list[Worker] = []

    async def ensure_switch(*_: object) -> None:
        calls.append("ensure_live_switch")

    async def connect(*_: object, **__: object) -> Client:
        calls.append("connect")
        return temporal_env.client

    async def fake_serve(workers: Sequence[Worker], stop: asyncio.Event) -> None:
        served.extend(workers)

    monkeypatch.setattr(worker_module, "ensure_live_switch", ensure_switch)
    monkeypatch.setattr(worker_module, "connect_client", connect)
    monkeypatch.setattr(worker_module, "serve", fake_serve)
    monkeypatch.setattr(worker_module, "stop_on_signals", lambda stop: None)
    await worker_module.run_worker(settings, graceful_shutdown=timedelta(seconds=5))

    assert calls == ["ensure_live_switch", "connect"]
    limits = {
        w.task_queue: (
            w.config().get("max_concurrent_activities"),
            w.config().get("max_task_queue_activities_per_second"),
            w.config().get("graceful_shutdown_timeout"),
        )
        for w in served
    }
    assert limits == {
        MAIN_TASK_QUEUE: (10, None, timedelta(seconds=5)),
        MODEL_TASK_QUEUE: (12, 5.0, timedelta(seconds=5)),
        CHAT_TASK_QUEUE: (4, None, timedelta(seconds=5)),
    }


async def test_harmony_runs_own_worker_inserts_the_live_switch_before_serving(
    temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker, app_settings: Settings
) -> None:
    """`harmony run --live` on a database no API or worker has booted against: without
    the switch row every live call would refuse."""
    settings = app_settings.model_copy(update={"app_env": "local", "model_mode": "live"})
    async with db_sessionmaker() as session, session.begin():
        await session.execute(delete(LiveControls))

    async with (
        running_worker(WorkerDeps(settings, temporal_env.client, db_sessionmaker)),
        db_sessionmaker() as session,
    ):
        paused = list(await session.scalars(select(LiveControls.paused)))

    assert paused == [False]


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
