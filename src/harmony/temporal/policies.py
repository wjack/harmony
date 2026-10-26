"""Task queues, retry policies and activity options, plus the world-status guard.

Workflow code imports this module inside `workflow.unsafe.imports_passed_through()`
(it imports the database layer for the guard) and unpacks the options into calls:

    await workflow.execute_activity_method(
        AgentActivities.rep_turn, turn, **model_activity_options()
    )

Non-retryable errors are matched by type name: an activity's uncaught exception reaches
the workflow as an `ApplicationError` whose `type` is the exception's class name
(`harmony.errors` explains why those names are a contract).
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityConfig

from harmony.db import repo
from harmony.errors import WorldRetired

# Workflows and short database activities run on `MAIN`; model calls run on `MODEL`,
# whose worker caps concurrency and rate so a burst of turns can't exhaust either the
# API rate limit or the database pool (§7).
MAIN_TASK_QUEUE = "harmony-main"
MODEL_TASK_QUEUE = "harmony-model"

WORLD_RETIRED = WorldRetired.__name__
# `harmony.llm.errors` classes, plus `GuardMismatch` (a model client paired with the
# wrong spend guard, `llm/checkpoint.py`): none of them can succeed on a retry. The
# Anthropic SDK's own 400 class is listed too, so an unwrapped 400 is never retried.
# `TurnIncomplete` replays the same checkpointed response on a retry, and a
# `TranscriptConflict` is a rewrite the store will refuse again (`agents/runtime.py`).
MODEL_NON_RETRYABLE = (
    "BadRequest",
    "BadRequestError",
    "BudgetExhausted",
    "LedgerUnavailable",
    "GuardMismatch",
    "TurnIncomplete",
    "TranscriptConflict",
    WORLD_RETIRED,
)

# The SDK client runs with `max_retries=1` and its own timeout; start_to_close must stay
# above twice that timeout, so a slow call is retried by the SDK before Temporal gives up.
MODEL_START_TO_CLOSE = timedelta(seconds=600)
# The activity heartbeats from a background task while the call streams, so worker
# shutdown and workflow termination cancel the call within this window.
MODEL_HEARTBEAT = timedelta(seconds=30)

MAIN_START_TO_CLOSE = timedelta(seconds=60)


def model_retry_policy() -> RetryPolicy:
    """Three attempts in all; a 400, an exhausted budget or a retired world stops at once."""
    return RetryPolicy(maximum_attempts=3, non_retryable_error_types=list(MODEL_NON_RETRYABLE))


def model_activity_options() -> ActivityConfig:
    """Options for one checkpointed model turn on `harmony-model` (§5)."""
    return ActivityConfig(
        task_queue=MODEL_TASK_QUEUE,
        start_to_close_timeout=MODEL_START_TO_CLOSE,
        heartbeat_timeout=MODEL_HEARTBEAT,
        retry_policy=model_retry_policy(),
    )


def main_retry_policy(*, maximum_attempts: int = 5) -> RetryPolicy:
    """Database activities retry transient failures, never a retired world.
    `maximum_attempts=0` retries until the activity succeeds."""
    return RetryPolicy(
        maximum_attempts=maximum_attempts,
        maximum_interval=timedelta(seconds=30),
        non_retryable_error_types=[WORLD_RETIRED],
    )


def main_activity_options(
    *,
    start_to_close: timedelta = MAIN_START_TO_CLOSE,
    schedule_to_close: timedelta | None = None,
    heartbeat: timedelta | None = None,
    retry_policy: RetryPolicy | None = None,
) -> ActivityConfig:
    """Options for a short database activity on `harmony-main`."""
    return ActivityConfig(
        task_queue=MAIN_TASK_QUEUE,
        start_to_close_timeout=start_to_close,
        schedule_to_close_timeout=schedule_to_close,
        heartbeat_timeout=heartbeat,
        retry_policy=retry_policy or main_retry_policy(),
    )


def mirror_activity_options() -> ActivityConfig:
    """Options for an activity that keeps the database and the chats in step with a
    workflow (a state, a notice): retried until it succeeds, so an outage delays the
    workflow instead of failing it and stranding whatever waits on it. A retired world
    or a domain error still stops at once."""
    return main_activity_options(retry_policy=main_retry_policy(maximum_attempts=0))


def world_retired_error(world_id: UUID) -> ApplicationError:
    """The guard's failure: non-retryable whatever the caller's retry policy says."""
    return ApplicationError(f"world {world_id} is not live", type=WORLD_RETIRED, non_retryable=True)


async def ensure_world_live(sessionmaker: async_sessionmaker[AsyncSession], world_id: UUID) -> None:
    """The status check every world activity runs first (§7).

    Raises a non-retryable `ApplicationError` of type `WorldRetired` unless the world is
    `live`. Commits re-check the status (`UnitOfWork`), so a retirement that lands after
    this check still stops the write, with the same error type.
    """
    async with sessionmaker() as session:
        status = await repo.world_status(session, world_id)
    if status != "live":
        raise world_retired_error(world_id)
