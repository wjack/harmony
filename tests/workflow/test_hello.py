"""HelloWorkflow on the time-skipping server, with real activities and a real database."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError, RetryState
from temporalio.testing import WorkflowEnvironment
from temporalio.workflow import ActivityConfig

from harmony.db.models import Event, WorldWorkflow
from harmony.errors import WorldRetired
from harmony.temporal.activities.hello import HelloActivities, HelloTickInput
from harmony.temporal.activities.retire_world import mark_world_retired
from harmony.temporal.policies import (
    MAIN_TASK_QUEUE,
    main_activity_options,
    model_activity_options,
)
from harmony.temporal.start import start_world_workflow, world_workflow_id
from harmony.temporal.workflows.hello import HelloInput, HelloResult, HelloWorkflow
from tests.integration._worlds import create_world
from tests.workflow._support import Sessionmaker, main_worker


async def test_hello_emits_three_ticks_with_timers_skipped(
    temporal_env_time_skipping: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    client = temporal_env_time_skipping.client
    world_id = await create_world(db_sessionmaker)  # world.created is seq 1
    workflow_id = world_workflow_id(world_id, "hello", "t")
    async with main_worker(client, db_sessionmaker):
        # Started directly: the time-skipping server has no custom search attributes.
        # `start_world_workflow` is covered on the local dev server (test_start.py).
        result = await client.execute_workflow(
            HelloWorkflow.run,
            # An hour between ticks: the test finishes only because time is skipped.
            HelloInput(world_id=world_id, interval=timedelta(hours=1)),
            id=workflow_id,
            task_queue=MAIN_TASK_QUEUE,
        )
    assert result == HelloResult(seqs=[2, 3, 4])

    async with db_sessionmaker() as session:
        ticks = (
            await session.scalars(
                select(Event.payload)
                .where(Event.world_id == world_id, Event.type == "hello.tick")
                .order_by(Event.seq)
            )
        ).all()
    assert ticks == [
        {"type": "hello.tick", "workflow_id": workflow_id, "n": n, "of": 3} for n in (1, 2, 3)
    ]


async def test_hello_through_the_start_helper_on_a_dev_server(
    temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    client = temporal_env.client
    world_id = await create_world(db_sessionmaker)
    workflow_id = world_workflow_id(world_id, "hello", "dev")
    async with main_worker(client, db_sessionmaker):
        handle = await start_world_workflow(
            client,
            db_sessionmaker,
            HelloWorkflow.run,
            HelloInput(world_id=world_id, interval=timedelta(milliseconds=50)),
            world_id=world_id,
            workflow_id=workflow_id,
        )
        assert await handle.result() == HelloResult(seqs=[2, 3, 4])

    async with db_sessionmaker() as session:
        recorded = await session.get(WorldWorkflow, (world_id, workflow_id))
    assert recorded is not None
    assert recorded.workflow_type == "HelloWorkflow"


async def test_a_retried_tick_returns_the_committed_seq(db_sessionmaker: Sessionmaker) -> None:
    world_id = await create_world(db_sessionmaker)
    activities = HelloActivities(db_sessionmaker)
    tick = HelloTickInput(world_id=world_id, workflow_id="w:x:hello:1", n=1, of=3)

    first = await activities.emit_hello_tick(tick)
    again = await activities.emit_hello_tick(tick)
    other = await activities.emit_hello_tick(tick.model_copy(update={"n": 2}))

    assert (first, again, other) == (2, 2, 3)


async def test_an_activity_on_a_retired_world_fails_the_workflow_without_retrying(
    temporal_env_time_skipping: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    client = temporal_env_time_skipping.client
    world_id = await create_world(db_sessionmaker)
    assert await mark_world_retired(db_sessionmaker, world_id)

    async with main_worker(client, db_sessionmaker):
        # start_world_workflow refuses a retired world, so start it the raw way: this is
        # the workflow that was already running when its world retired.
        handle = await client.start_workflow(
            HelloWorkflow.run,
            HelloInput(world_id=world_id),
            id=world_workflow_id(world_id, "hello", "late"),
            task_queue=MAIN_TASK_QUEUE,
        )
        with pytest.raises(WorkflowFailureError) as failure:
            await handle.result()

    _assert_failed_once_as_world_retired(failure.value)
    history = await handle.fetch_history()
    attempts = [
        event.activity_task_started_event_attributes.attempt
        for event in history.events
        if event.HasField("activity_task_started_event_attributes")
    ]
    assert attempts == [1]


# --- The commit-time path: an activity raising harmony.errors.WorldRetired ---------


class _RetiredAtCommit:
    """Raises what a UnitOfWork raises when the world retires between guard and commit."""

    def __init__(self) -> None:
        self.calls = 0

    @activity.defn
    async def commit_into_retired_world(self, world_id: UUID) -> None:
        self.calls += 1
        raise WorldRetired(world_id)


@workflow.defn(sandboxed=False)
class _CommitWorkflow:
    @workflow.run
    async def run(self, use_model_options: bool) -> None:
        options: ActivityConfig = (
            model_activity_options() if use_model_options else main_activity_options()
        )
        options["task_queue"] = MAIN_TASK_QUEUE
        await workflow.execute_activity_method(
            _RetiredAtCommit.commit_into_retired_world, uuid4(), **options
        )


@pytest.mark.parametrize("use_model_options", [False, True], ids=["main", "model"])
async def test_world_retired_at_commit_is_never_retried(
    temporal_env_time_skipping: WorkflowEnvironment,
    db_sessionmaker: Sessionmaker,
    use_model_options: bool,
) -> None:
    client = temporal_env_time_skipping.client
    raiser = _RetiredAtCommit()
    async with main_worker(
        client,
        db_sessionmaker,
        workflows=[_CommitWorkflow],
        activities=[raiser.commit_into_retired_world],
    ):
        with pytest.raises(WorkflowFailureError) as failure:
            await client.execute_workflow(
                _CommitWorkflow.run,
                use_model_options,
                id=f"commit-{uuid4()}",
                task_queue=MAIN_TASK_QUEUE,
            )

    _assert_failed_once_as_world_retired(failure.value)
    assert raiser.calls == 1


def _assert_failed_once_as_world_retired(failure: WorkflowFailureError) -> None:
    activity_error = failure.cause
    assert isinstance(activity_error, ActivityError)
    assert activity_error.retry_state == RetryState.NON_RETRYABLE_FAILURE
    cause = activity_error.cause
    assert isinstance(cause, ApplicationError)
    assert cause.type == "WorldRetired"
