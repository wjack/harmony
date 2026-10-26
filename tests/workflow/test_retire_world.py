"""RetireWorldWorkflow on a local dev server, whose List Workflow Executions API the sweep
needs."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.testing import WorkflowEnvironment

from harmony.db.models import Event, World, WorldWorkflow
from harmony.temporal.activities.retire_world import (
    delete_retired_world_rows,
    mark_world_retired,
    terminate_world_workflows,
)
from harmony.temporal.policies import MAIN_TASK_QUEUE
from harmony.temporal.search_attributes import WORLD_ID, world_search_attributes
from harmony.temporal.start import start_world_workflow, world_workflow_id
from harmony.temporal.workflows.retire_world import (
    RetireWorldInput,
    RetireWorldWorkflow,
    retire_workflow_id,
)
from harmony.worlds.retirer import Retirer
from harmony.worlds.retirer_temporal import TemporalRetirer
from tests.integration._worlds import create_world
from tests.workflow._support import (
    ParkedWorkflow,
    Sessionmaker,
    count_rows,
    exists_in_temporal,
    main_worker,
    world_status,
)


async def _status(client: Client, workflow_id: str) -> WorkflowExecutionStatus | None:
    return (await client.get_workflow_handle(workflow_id).describe()).status


async def test_retirement_terminates_one_worlds_workflows_and_deletes_its_rows(
    temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    client = temporal_env.client
    world_a, world_b = await create_world(db_sessionmaker), await create_world(db_sessionmaker)

    async def park(world_id: UUID, name: str) -> str:
        workflow_id = world_workflow_id(world_id, "conv", name)
        await start_world_workflow(
            client,
            db_sessionmaker,
            ParkedWorkflow.run,
            name,
            world_id=world_id,
            workflow_id=workflow_id,
        )
        return workflow_id

    async with main_worker(client, db_sessionmaker):
        in_a = [await park(world_a, f"a{n}") for n in range(3)]
        in_b = await park(world_b, "b0")

        # A root workflow the sweep can't see (no WorldId), which only its recorded ID
        # finds, and a recorded ID Temporal never heard of, which must not fail anything.
        hidden = world_workflow_id(world_a, "req", "hidden")
        await client.start_workflow(ParkedWorkflow.run, "h", id=hidden, task_queue=MAIN_TASK_QUEUE)
        async with db_sessionmaker() as session, session.begin():
            for workflow_id in (hidden, world_workflow_id(world_a, "thread", "gone")):
                session.add(
                    WorldWorkflow(
                        world_id=world_a, workflow_id=workflow_id, workflow_type="ParkedWorkflow"
                    )
                )

        await TemporalRetirer(client, db_sessionmaker, row_ttl=timedelta(0)).retire(world_a)
        retire = client.get_workflow_handle_for(
            RetireWorldWorkflow.run, retire_workflow_id(world_a)
        )
        result = await retire.result()

        assert (result.world_found, result.rows_deleted) == (True, True)
        assert sorted(result.terminated) == sorted([*in_a, hidden])
        for workflow_id in [*in_a, hidden]:
            assert await _status(client, workflow_id) == WorkflowExecutionStatus.TERMINATED
        assert await _status(client, in_b) == WorkflowExecutionStatus.RUNNING

        # The retire workflow completed on its own: it never listed or terminated itself.
        description = await retire.describe()
        assert description.status == WorkflowExecutionStatus.COMPLETED
        assert description.typed_search_attributes.get(WORLD_ID) is None

    async with db_sessionmaker() as session:
        world = await session.get_one(World, world_a)
    assert world.status == "retired"
    assert world.retired_at is not None
    assert await count_rows(db_sessionmaker, Event, world_a) == 0
    assert await count_rows(db_sessionmaker, WorldWorkflow, world_a) == 0

    assert await world_status(db_sessionmaker, world_b) == "live"
    assert await count_rows(db_sessionmaker, WorldWorkflow, world_b) == 1
    assert await count_rows(db_sessionmaker, Event, world_b) == 1


async def test_the_sweep_finds_workflows_that_were_never_recorded(
    temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    """Child workflows aren't recorded; the WorldId sweep is what catches them."""
    client = temporal_env.client
    world_id = await create_world(db_sessionmaker)
    child = world_workflow_id(world_id, "thread", "child")
    async with main_worker(client, db_sessionmaker):
        await client.start_workflow(
            ParkedWorkflow.run,
            "c",
            id=child,
            task_queue=MAIN_TASK_QUEUE,
            search_attributes=world_search_attributes(world_id),
        )
        terminated = await terminate_world_workflows(
            client,
            db_sessionmaker,
            world_id,
            reason="test",
            settle_interval=timedelta(seconds=0.1),
        )
        assert terminated == [child]
        assert await _status(client, child) == WorkflowExecutionStatus.TERMINATED


async def test_marking_retired_is_idempotent_and_emits_world_retired_once(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await create_world(db_sessionmaker)

    assert await mark_world_retired(db_sessionmaker, world_id)
    assert await mark_world_retired(db_sessionmaker, world_id)

    async with db_sessionmaker() as session:
        types = (
            await session.scalars(
                select(Event.type).where(Event.world_id == world_id).order_by(Event.seq)
            )
        ).all()
    assert types == ["world.created", "world.retired"]
    assert await world_status(db_sessionmaker, world_id) == "retired"


async def test_marking_an_unknown_world_reports_it_missing(db_sessionmaker: Sessionmaker) -> None:
    assert not await mark_world_retired(db_sessionmaker, uuid4())


async def test_rows_are_deleted_only_once_retired(db_sessionmaker: Sessionmaker) -> None:
    world_id = await create_world(db_sessionmaker)
    with pytest.raises(ValueError, match="not retired"):
        await delete_retired_world_rows(db_sessionmaker, world_id)
    assert await count_rows(db_sessionmaker, Event, world_id) == 1


async def test_the_retirer_marks_the_world_retiring_before_the_workflow_runs(
    temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    """No worker is polling, so on return only the retirer's own step has happened."""
    client = temporal_env.client
    world_id = await create_world(db_sessionmaker)
    retirer: Retirer = TemporalRetirer(client, db_sessionmaker)

    await retirer.retire(world_id)
    await retirer.retire(world_id)  # reuses the running retirement

    assert await world_status(db_sessionmaker, world_id) == "retiring"
    description = await client.get_workflow_handle(retire_workflow_id(world_id)).describe()
    assert description.status == WorkflowExecutionStatus.RUNNING
    assert description.workflow_type == "RetireWorldWorkflow"


async def test_retiring_an_unknown_world_starts_nothing(
    temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    world_id = uuid4()
    await TemporalRetirer(temporal_env.client, db_sessionmaker).retire(world_id)
    assert not await exists_in_temporal(temporal_env.client, retire_workflow_id(world_id))


def test_retire_input_defaults_to_immediate_deletion() -> None:
    assert RetireWorldInput(world_id=uuid4()).row_ttl == timedelta(0)
