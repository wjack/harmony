"""The in-process Temporal worker `harmony run` plays the story on.

Workflows and activities go to whichever worker polls the task queue, so with another
worker polling (`make dev`, or a second `harmony run`) some of the story's turns would
run there, in that worker's model mode and with its cassettes. The run refuses to start
its own worker then.

`ActivityLog` reads what the worker logs about failed activities, so the run can stop
on a model call that failed for good instead of waiting out its timeout, and the report
can count retries and failures by kind.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections import Counter
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from temporalio import activity
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client
from temporalio.exceptions import ApplicationError

from harmony.llm.live_switch import ensure_live_switch
from harmony.temporal.policies import (
    CHAT_TASK_QUEUE,
    MAIN_TASK_QUEUE,
    MODEL_NON_RETRYABLE,
    MODEL_TASK_QUEUE,
    WORLD_RETIRED,
    model_retry_policy,
)
from harmony.temporal.start import world_workflow_id
from harmony.temporal.worker import WorkerConfig, WorkerDeps, build_workers, serve
from harmony.temporal.workflows.payloads import BUDGET_EXHAUSTED_ERROR

# A poller seen this recently is a live worker (pollers long-poll for about a minute).
RECENT_POLL = timedelta(seconds=90)
MODEL_QUEUES = (MODEL_TASK_QUEUE, CHAT_TASK_QUEUE)
QUEUES = (MAIN_TASK_QUEUE, *MODEL_QUEUES)


class WorkerBusy(RuntimeError):
    """Another worker is polling the task queues this run would serve."""


def identity(label: str) -> str:
    return f"{os.getpid()}@{socket.gethostname()} {label}"


async def active_pollers(
    client: Client, queues: Sequence[str] = QUEUES, *, now: datetime | None = None
) -> list[str]:
    """`<identity> on <queue>` for every poller seen within `RECENT_POLL`."""
    cutoff = (now or datetime.now(UTC)) - RECENT_POLL
    found: list[str] = []
    for queue in queues:
        for kind in (
            TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
            TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
        ):
            response = await client.workflow_service.describe_task_queue(
                DescribeTaskQueueRequest(
                    namespace=client.namespace,
                    task_queue=TaskQueue(name=queue),
                    task_queue_type=kind,
                )
            )
            for poller in response.pollers:
                seen = poller.last_access_time.ToDatetime(tzinfo=UTC)
                label = f"{poller.identity} on {queue}"
                if seen >= cutoff and not exited(poller.identity) and label not in found:
                    found.append(label)
    return found


def exited(poller_identity: str) -> bool:
    """Whether a poller was a process on this machine that has since exited. Temporal
    keeps listing a poller for a while after it stops, such as the previous run's."""
    pid, _, rest = poller_identity.partition("@")
    host = rest.split(" ", 1)[0]
    if host != socket.gethostname() or not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


async def ensure_no_pollers(client: Client) -> None:
    pollers = await active_pollers(client)
    if pollers:
        raise WorkerBusy(
            "another worker is polling harmony's task queues ("
            + "; ".join(pollers)
            + "). harmony run plays the story on its own worker: stop that one first."
        )


# A model call that failed with one of these won't succeed on a retry, so its turn is
# lost (`temporal.policies`). A retired world is the exception: that's the run ending.
FATAL_MODEL_ERRORS = frozenset(MODEL_NON_RETRYABLE) - {WORLD_RETIRED}
MODEL_ATTEMPTS = model_retry_policy().maximum_attempts


def error_type(error: BaseException) -> str:
    """The type name retry policies match: an `ApplicationError`'s `type`, else the
    exception's class name."""
    if isinstance(error, ApplicationError) and error.type:
        return error.type
    return type(error).__name__


def error_label(error: BaseException) -> str:
    """`error_type`, with the refusing cap of a refused reservation: `BudgetExhausted
    (session)`."""
    kind = error_type(error)
    if kind != BUDGET_EXHAUSTED_ERROR or not isinstance(error, ApplicationError):
        return kind
    details = error.details[0] if error.details else None
    cap = cast(Mapping[str, object], details).get("kind") if isinstance(details, Mapping) else None
    return f"{kind} ({cap})" if isinstance(cap, str) else kind


class ActivityLog(logging.Filter):
    """A filter on `temporalio.activity`, where the worker logs every failed activity
    attempt with its error and its workflow (`temporal_activity`).

    - `failures`: workflow ID → the first model call in it that failed for good: a
      non-retryable error, or its last attempt.
    - `errors`: every failed model-activity attempt, by `error_label`.
    - `retries`: the failed model-activity attempts Temporal tries again.
    - Not logged: a `WorldRetired` failure, which is a retired world's last activities
      stopping (an older world's still on the queue, say), and any failure in a world
      `retiring` was called for, whose rows the retirement is removing. Their
      tracebacks would read as faults after the run.
    """

    def __init__(self) -> None:
        super().__init__()
        self.failures: dict[str, str] = {}
        self.errors: Counter[str] = Counter()
        self.retries = 0
        self._retiring: set[str] = set()

    def retiring(self, world_id: UUID) -> None:
        # `w:{world}:`, the prefix of every workflow ID in the world.
        self._retiring.add(world_workflow_id(world_id, ""))

    def filter(self, record: logging.LogRecord) -> bool:
        error = record.exc_info[1] if record.exc_info else None
        if error is None:
            return True
        kind = error_type(error)
        details: object = getattr(record, "temporal_activity", None)
        info: Mapping[str, object] = (
            cast(Mapping[str, object], details) if isinstance(details, Mapping) else {}
        )
        workflow_id = info.get("workflow_id")
        workflow_id = workflow_id if isinstance(workflow_id, str) else ""
        if kind == WORLD_RETIRED or workflow_id.startswith(tuple(self._retiring)):
            return False
        if info.get("task_queue") not in MODEL_QUEUES:
            return True
        self.errors[error_label(error)] += 1
        attempt = info.get("attempt")
        last = isinstance(attempt, int) and attempt >= MODEL_ATTEMPTS
        if kind not in FATAL_MODEL_ERRORS and not last:
            self.retries += 1
        elif workflow_id:
            message = error.message if isinstance(error, ApplicationError) else str(error)
            self.failures.setdefault(workflow_id, f"{kind}: {message}")
        return True


@dataclass(frozen=True, slots=True)
class InProcessWorker:
    # Done early only if a worker failed; its failure is re-raised when the block exits.
    serving: asyncio.Task[None]
    log: ActivityLog = field(default_factory=ActivityLog)


@asynccontextmanager
async def running_worker(deps: WorkerDeps) -> AsyncGenerator[InProcessWorker]:
    """Serve `deps`' workers until the block exits, with their activity failures read
    by the yielded `InProcessWorker.log`. The live switch row is made sure of first, as
    for `harmony worker` (`ensure_live_switch`)."""
    log = ActivityLog()
    logger = activity.logger.base_logger
    await ensure_live_switch(deps.sessionmaker)
    logger.addFilter(log)
    stop = asyncio.Event()
    workers = build_workers(deps, WorkerConfig.from_settings(deps.settings))
    worker = InProcessWorker(asyncio.create_task(serve(workers, stop)), log)
    try:
        yield worker
    finally:
        stop.set()
        try:
            await worker.serving
        finally:
            logger.removeFilter(log)
