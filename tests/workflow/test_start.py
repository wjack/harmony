"""`start_world_workflow`: attributes, run timeout, the record, and refusals."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import update
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment

from harmony.db.models import World, WorldWorkflow
from harmony.errors import WorldRetired
from harmony.temporal.search_attributes import ORG_ID, WORLD_ID
from harmony.temporal.start import (
    WORLD_RUN_TIMEOUT,
    WorldNotFound,
    start_world_workflow,
    world_workflow_id,
)
from tests.integration._worlds import create_world
from tests.workflow._support import (
    ParkedWorkflow,
    Sessionmaker,
    count_rows,
    exists_in_temporal,
    main_worker,
)


async def test_start_sets_attributes_and_run_timeout_and_records_the_workflow(
    temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    client = temporal_env.client
    world_id, org_id = await create_world(db_sessionmaker), uuid4()
    workflow_id = world_workflow_id(world_id, "conv", "c1")
    async with main_worker(client, db_sessionmaker):
        handle = await start_world_workflow(
            client,
            db_sessionmaker,
            ParkedWorkflow.run,
            "parked",
            world_id=world_id,
            workflow_id=workflow_id,
            org_id=org_id,
        )
        description = await handle.describe()
        assert description.status == WorkflowExecutionStatus.RUNNING
        assert description.typed_search_attributes.get(WORLD_ID) == str(world_id)
        assert description.typed_search_attributes.get(ORG_ID) == str(org_id)
        config = description.raw_description.execution_config
        assert config.workflow_run_timeout.ToTimedelta() == WORLD_RUN_TIMEOUT == timedelta(hours=24)
        # The backstop spans the whole chain: a run timeout alone restarts with every
        # continue-as-new.
        assert config.workflow_execution_timeout.ToTimedelta() == WORLD_RUN_TIMEOUT

        # Signal-with-start on the same ID reuses the run and records nothing new.
        again = await start_world_workflow(
            client,
            db_sessionmaker,
            ParkedWorkflow.run,
            "parked",
            world_id=world_id,
            workflow_id=workflow_id,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            start_signal="release",
        )
        assert await again.result() == "parked"
        assert again.result_run_id == handle.result_run_id

    async with db_sessionmaker() as session:
        recorded = await session.get(WorldWorkflow, (world_id, workflow_id))
    assert recorded is not None
    assert recorded.workflow_type == "ParkedWorkflow"
    assert await count_rows(db_sessionmaker, WorldWorkflow, world_id) == 1


async def test_an_org_less_start_sets_only_world_id(
    temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    client = temporal_env.client
    world_id = await create_world(db_sessionmaker)
    handle = await start_world_workflow(
        client,
        db_sessionmaker,
        ParkedWorkflow.run,
        "x",
        world_id=world_id,
        workflow_id=world_workflow_id(world_id, "replay"),
    )
    attributes = (await handle.describe()).typed_search_attributes
    assert attributes.get(WORLD_ID) == str(world_id)
    assert attributes.get(ORG_ID) is None
    await handle.terminate()


@pytest.mark.parametrize("status", ["retiring", "retired"])
async def test_start_refuses_a_world_that_is_not_live(
    temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker, status: str
) -> None:
    client = temporal_env.client
    world_id = await create_world(db_sessionmaker)
    async with db_sessionmaker() as session, session.begin():
        await session.execute(update(World).where(World.id == world_id).values(status=status))
    workflow_id = world_workflow_id(world_id, "conv", "c1")

    with pytest.raises(WorldRetired):
        await start_world_workflow(
            client,
            db_sessionmaker,
            ParkedWorkflow.run,
            "x",
            world_id=world_id,
            workflow_id=workflow_id,
        )

    assert not await exists_in_temporal(client, workflow_id)
    assert await count_rows(db_sessionmaker, WorldWorkflow, world_id) == 0


async def test_start_refuses_an_unknown_world(
    temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    world_id = uuid4()
    with pytest.raises(WorldNotFound):
        await start_world_workflow(
            temporal_env.client,
            db_sessionmaker,
            ParkedWorkflow.run,
            "x",
            world_id=world_id,
            workflow_id=world_workflow_id(world_id, "conv", "c1"),
        )


async def test_start_requires_the_worlds_id_prefix(
    temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    world_id, other = await create_world(db_sessionmaker), uuid4()
    for bad in (f"w:{other}:conv:c1", f"w:{world_id}", f"retire:{world_id}"):
        with pytest.raises(ValueError, match="must start with"):
            await start_world_workflow(
                temporal_env.client,
                db_sessionmaker,
                ParkedWorkflow.run,
                "x",
                world_id=world_id,
                workflow_id=bad,
            )
    assert await count_rows(db_sessionmaker, WorldWorkflow, world_id) == 0


async def test_a_failed_start_leaves_no_record(
    temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    client = temporal_env.client
    world_id = await create_world(db_sessionmaker)
    workflow_id = world_workflow_id(world_id, "conv", "c1")
    first = await start_world_workflow(
        client, db_sessionmaker, ParkedWorkflow.run, "x", world_id=world_id, workflow_id=workflow_id
    )
    async with db_sessionmaker() as session, session.begin():
        await session.delete(await session.get_one(WorldWorkflow, (world_id, workflow_id)))

    # The default conflict policy refuses a second run while the first is running.
    with pytest.raises(WorkflowAlreadyStartedError):
        await start_world_workflow(
            client,
            db_sessionmaker,
            ParkedWorkflow.run,
            "x",
            world_id=world_id,
            workflow_id=workflow_id,
        )
    assert await count_rows(db_sessionmaker, WorldWorkflow, world_id) == 0
    await first.terminate()
