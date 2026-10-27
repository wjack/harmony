"""The Temporal worker process: one client, one worker per task queue.

- `harmony-main` runs every workflow and the short database activities.
- `harmony-model` runs the representatives' and answerers' model activities, with
  tighter concurrency and a task-queue-wide rate limit (§7).
- `harmony-chat` runs internal Claude's turns, on slots of their own, so a fan-out of
  threads can't queue every visitor's typed chat (3.7).

The model queues only start once something is registered on them. Their capacity comes
from settings (`WorkerConfig.from_settings`), so the CLI can't bypass it.

Model activities call through a `ModelRuntime`: the model client for MODEL_MODE
(`ScriptedModel` over the cassettes, or the Anthropic client), its spend guard, and the
process's one token-stream publisher. Building it first means a live worker without its
spend ledger refuses to start (§14).

Register new workflows and activities in the `*_workflows` / `*_activities` functions
below. Activities are bound methods of classes that receive their dependencies, so
tests build the same objects with their own database and client.

At start the worker makes sure the hosted spend rows exist (`ensure_hosted_rows`), and
with the hosted caps on it settles reservations a dead process left open
(`settle_stale_reservations`), at start and every `SETTLE_STALE_EVERY`.

SIGTERM (Render) and SIGINT (Ctrl-C, watchfiles reloads) start a graceful shutdown:
pollers stop, running activities get `graceful_shutdown` to finish before they are
cancelled. A second signal falls through to the default handler and exits at once.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.worker import Worker

from harmony.config import Settings
from harmony.db.engine import create_sessionmaker, engine_from_settings
from harmony.events.bus import StreamPublisher
from harmony.events.stream import PgStreamPublisher
from harmony.llm import ModelClient
from harmony.llm.budget import SpendGuard, spend_guard
from harmony.llm.budget_pg import HostedBudgets, HostedCaps, ensure_hosted_rows
from harmony.llm.scripted import ScriptedModel
from harmony.temporal.activities.agents import AgentActivities
from harmony.temporal.activities.hello import HelloActivities
from harmony.temporal.activities.network import NetworkActivities
from harmony.temporal.activities.retire_world import RetireWorldActivities
from harmony.temporal.client import connect_client
from harmony.temporal.policies import CHAT_TASK_QUEUE, MAIN_TASK_QUEUE, MODEL_TASK_QUEUE
from harmony.temporal.workflows.conversation import ConversationWorkflow
from harmony.temporal.workflows.hello import HelloWorkflow
from harmony.temporal.workflows.network_request import NetworkRequestWorkflow
from harmony.temporal.workflows.retire_world import RetireWorldWorkflow
from harmony.temporal.workflows.thread import ThreadWorkflow

logger = logging.getLogger(__name__)

GRACEFUL_SHUTDOWN: Final = timedelta(seconds=90)
# A reservation still open after this was left by a process that died mid-call. It must
# outlast the model activity's timeout (`policies.MODEL_START_TO_CLOSE`), so a live call
# is never settled under it (3.6).
SETTLE_STALE_AFTER: Final = timedelta(minutes=15)
SETTLE_STALE_EVERY: Final = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Capacity limits. Defaults match the local settings and fit the connection budget
    in `harmony.config` (8 + 4 pooled connections): a model call holds no session."""

    main_max_concurrent_activities: int = 10
    model_max_concurrent_activities: int = 4
    # Across every worker polling `harmony-model`, enforced by the server.
    model_activities_per_second: float = 2.0
    chat_max_concurrent_activities: int = 2
    max_cached_workflows: int = 200
    graceful_shutdown: timedelta = GRACEFUL_SHUTDOWN

    @classmethod
    def from_settings(
        cls, settings: Settings, *, graceful_shutdown: timedelta = GRACEFUL_SHUTDOWN
    ) -> WorkerConfig:
        """The model and chat capacity the deployment configured (3.7)."""
        return cls(
            model_max_concurrent_activities=settings.worker_model_concurrency,
            model_activities_per_second=settings.worker_model_rate,
            chat_max_concurrent_activities=settings.worker_chat_concurrency,
            graceful_shutdown=graceful_shutdown,
        )


# Scripted turns stream at a readable pace, so the UI's live text can be followed in dev.
SCRIPTED_EVENT_DELAY_S = 0.02


@dataclass(frozen=True, slots=True)
class ModelRuntime:
    """What model activities call through. Built once per worker process."""

    client: ModelClient
    guard: SpendGuard
    publisher: StreamPublisher


@dataclass(frozen=True, slots=True)
class WorkerDeps:
    """What activity classes are built from; tests pass their own database and client.

    Without `model`, nothing is registered on `harmony-model` or `harmony-chat`.
    """

    settings: Settings
    client: Client
    sessionmaker: async_sessionmaker[AsyncSession]
    model: ModelRuntime | None = None


def scripted_model(settings: Settings) -> ScriptedModel:
    """`ScriptedModel` over every cassette in `CASSETTE_DIR`.

    A hosted or CI worker without them refuses to start: every turn would fail.
    """
    if not settings.cassette_dir.is_dir():
        if settings.app_env != "local":
            raise RuntimeError(
                f"no cassettes at {settings.cassette_dir}; MODEL_MODE=scripted plays them "
                "(the image ships tests/fixtures/cassettes)"
            )
        logger.warning(
            "no cassettes at %s: every scripted model call will fail", settings.cassette_dir
        )
        return ScriptedModel({}, event_delay_s=SCRIPTED_EVENT_DELAY_S)
    return ScriptedModel.from_paths(settings.cassette_dir, event_delay_s=SCRIPTED_EVENT_DELAY_S)


def model_runtime(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> ModelRuntime:
    """The client for MODEL_MODE with its guard (`FreeSpend` when scripted, so a scripted
    run never writes a ledger line), and the worker's stream publisher."""
    guard = spend_guard(settings, sessionmaker=sessionmaker)
    client: ModelClient
    if settings.model_mode == "scripted":
        client = scripted_model(settings)
    else:
        # Imported only here: the Anthropic SDK is the one heavy import in the worker.
        from harmony.llm.anthropic_model import AnthropicModel

        # A provider spend-limit refusal blocks later hosted calls locally (3.4).
        client = AnthropicModel.from_settings(settings, on_provider_limit=guard.block_provider)
    return ModelRuntime(client, guard, PgStreamPublisher.from_settings(settings))


def main_workflows() -> list[type]:
    return [
        HelloWorkflow,
        RetireWorldWorkflow,
        ConversationWorkflow,
        NetworkRequestWorkflow,
        ThreadWorkflow,
    ]


def main_activities(deps: WorkerDeps) -> list[Callable[..., Any]]:
    hello = HelloActivities(deps.sessionmaker)
    retire = RetireWorldActivities(deps.client, deps.sessionmaker)
    network = NetworkActivities(deps.client, deps.sessionmaker)
    return [
        hello.emit_hello_tick,
        retire.mark_retired,
        retire.terminate_workflows,
        retire.delete_rows,
        network.apply_card_resolution,
        network.settled_card,
        network.start_network_request,
        network.open_thread,
        network.apply_resolution,
        network.stored_resolution,
        network.reassign_escalation,
        network.report_redraft_failed,
        network.set_thread_state,
        network.note_unsummarized,
        network.set_request_status,
        network.notify_conversation,
    ]


def agent_activities(deps: WorkerDeps) -> AgentActivities | None:
    if deps.model is None:
        return None
    return AgentActivities(
        deps.sessionmaker,
        deps.model.client,
        deps.model.guard,
        deps.model.publisher,
        model_override=deps.settings.model_override,
        # Hosted live calls are also capped per session lineage (§14).
        hosted_caps=HostedCaps.from_settings(deps.settings),
    )


def model_activities(agents: AgentActivities | None) -> list[Callable[..., Any]]:
    if agents is None:
        return []
    return [agents.rep_turn, agents.answer_consult]


def chat_activities(agents: AgentActivities | None) -> list[Callable[..., Any]]:
    if agents is None:
        return []
    return [agents.internal_turn]


def build_workers(deps: WorkerDeps, config: WorkerConfig | None = None) -> list[Worker]:
    """One worker per task queue that has something registered on it."""
    config = config or WorkerConfig()
    agents = agent_activities(deps)
    workers = [
        Worker(
            deps.client,
            task_queue=MAIN_TASK_QUEUE,
            workflows=main_workflows(),
            activities=main_activities(deps),
            max_concurrent_activities=config.main_max_concurrent_activities,
            max_cached_workflows=config.max_cached_workflows,
            graceful_shutdown_timeout=config.graceful_shutdown,
        )
    ]
    if model := model_activities(agents):
        workers.append(
            Worker(
                deps.client,
                task_queue=MODEL_TASK_QUEUE,
                activities=model,
                max_concurrent_activities=config.model_max_concurrent_activities,
                max_task_queue_activities_per_second=config.model_activities_per_second,
                graceful_shutdown_timeout=config.graceful_shutdown,
            )
        )
    if chat := chat_activities(agents):
        workers.append(
            Worker(
                deps.client,
                task_queue=CHAT_TASK_QUEUE,
                activities=chat,
                max_concurrent_activities=config.chat_max_concurrent_activities,
                graceful_shutdown_timeout=config.graceful_shutdown,
            )
        )
    return workers


async def serve(workers: Sequence[Worker], stop: asyncio.Event) -> None:
    """Run `workers` until `stop` is set or one of them fails, then shut all down.

    Re-raises the first worker failure, so a fatal error exits the process non-zero
    and the platform restarts it.
    """
    runs = [asyncio.create_task(worker.run()) for worker in workers]
    stopped = asyncio.create_task(stop.wait())
    await asyncio.wait([*runs, stopped], return_when=asyncio.FIRST_COMPLETED)

    # `shutdown()` waits for `run()` to finish; a worker whose `run()` failed before it
    # started never finishes shutting down, so those waits are cancelled below.
    shutdowns = [asyncio.create_task(worker.shutdown()) for worker in workers]
    results = await asyncio.gather(*runs, return_exceptions=True)
    stopped.cancel()
    for task in shutdowns:
        task.cancel()
    await asyncio.gather(*shutdowns, return_exceptions=True)
    await asyncio.wait([stopped])

    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        raise failures[0]


def stop_on_signals(stop: asyncio.Event) -> None:
    """Set `stop` on the first SIGTERM or SIGINT, then restore the default handlers.

    watchfiles reloads the dev worker with SIGINT, Render stops it with SIGTERM.
    """
    loop = asyncio.get_running_loop()

    def request_stop(sig: signal.Signals) -> None:
        logger.info("%s received: shutting down (send it again to exit now)", sig.name)
        for each in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(each)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, request_stop, sig)


class StaleSettler(Protocol):
    async def settle_stale(self, older_than: timedelta) -> int: ...


async def settle_stale_reservations(
    budgets: StaleSettler,
    *,
    every: timedelta = SETTLE_STALE_EVERY,
    older_than: timedelta = SETTLE_STALE_AFTER,
) -> None:
    """Settle reservations a dead process left open, now and then every `every`, until
    cancelled. A failed sweep is logged and tried again on the next."""
    while True:
        try:
            settled = await budgets.settle_stale(older_than)
        except Exception:
            logger.exception("settling stale budget reservations failed")
        else:
            if settled:
                logger.warning("settled %d stale budget reservations at their estimate", settled)
        await asyncio.sleep(every.total_seconds())


@asynccontextmanager
async def stale_reservation_sweep(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncGenerator[None]:
    """`settle_stale_reservations` for the block's duration when the hosted caps are on;
    without them nothing reserves against Postgres, so nothing runs."""
    if HostedCaps.from_settings(settings) is None:
        yield
        return
    sweep = asyncio.create_task(settle_stale_reservations(HostedBudgets(sessionmaker)))
    try:
        yield
    finally:
        sweep.cancel()
        with suppress(asyncio.CancelledError):
            await sweep


@asynccontextmanager
async def hosted_spend(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncGenerator[None]:
    """What every worker sets up for hosted spend before it serves, `harmony worker` and
    `harmony run`'s own alike: the live switch and the configured global and daily caps
    (`ensure_hosted_rows`; without the `global` row every hosted call refuses), then the
    stale-reservation sweep for the block's duration."""
    await ensure_hosted_rows(sessionmaker, settings)
    async with stale_reservation_sweep(settings, sessionmaker):
        yield


async def run_worker(
    settings: Settings, *, graceful_shutdown: timedelta = GRACEFUL_SHUTDOWN
) -> None:
    """The `harmony worker` process: connect, serve until signalled, clean up. Capacity
    comes from `settings`; the caller picks only how long a shutdown may take."""
    config = WorkerConfig.from_settings(settings, graceful_shutdown=graceful_shutdown)
    logger.info(
        "harmony worker starting: build %s, temporal %s namespace %s",
        settings.build_sha,
        settings.temporal_address,
        settings.temporal_namespace,
    )
    # The build SHA in the identity shows, per poller, which deploy is serving a queue.
    identity = f"{os.getpid()}@{socket.gethostname()} harmony-worker {settings.build_sha[:12]}"
    engine = engine_from_settings(settings, application_name="harmony-worker")
    sessionmaker = create_sessionmaker(engine)
    try:
        # Before connecting: a live worker without its ledger must not start at all.
        runtime = model_runtime(settings, sessionmaker)
        try:
            async with hosted_spend(settings, sessionmaker):
                client = await connect_client(settings, identity=identity)
                deps = WorkerDeps(settings, client, sessionmaker, runtime)
                workers = build_workers(deps, config)
                logger.info(
                    "polling task queues %s (model mode %s; model slots %d at %g/s, chat slots %d)",
                    ", ".join(worker.task_queue for worker in workers),
                    settings.model_mode,
                    config.model_max_concurrent_activities,
                    config.model_activities_per_second,
                    config.chat_max_concurrent_activities,
                )
                stop = asyncio.Event()
                stop_on_signals(stop)
                await serve(workers, stop)
        finally:
            await runtime.publisher.aclose()
    finally:
        await engine.dispose()
    logger.info("harmony worker stopped")
