"""The API-startup check: retire live worlds whose recorded workflows no longer exist,
and finish retirements left `retiring`."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import update
from temporalio.client import Client
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment

from harmony.db.models import World, WorldWorkflow
from harmony.temporal.start import start_world_workflow, world_workflow_id
from harmony.worlds.retirer_temporal import _any_known, retire_orphaned_worlds  # pyright: ignore[reportPrivateUsage]
from tests.integration._worlds import create_world
from tests.workflow._support import ParkedWorkflow, Sessionmaker, main_worker


class _RecordingRetirer:
    def __init__(self) -> None:
        self.retired: list[UUID] = []

    async def retire(self, world_id: UUID) -> None:
        self.retired.append(world_id)


async def _record(sessionmaker: Sessionmaker, world_id: UUID, *names: str) -> None:
    async with sessionmaker() as session, session.begin():
        for name in names:
            session.add(
                WorldWorkflow(
                    world_id=world_id,
                    workflow_id=world_workflow_id(world_id, "conv", name),
                    workflow_type="ParkedWorkflow",
                )
            )


async def test_only_worlds_whose_workflows_are_all_gone_are_retired(
    temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    """...plus every world left `retiring` (its retirement never started, or failed),
    whatever Temporal knows of its workflows."""
    client = temporal_env.client
    orphaned, running, finished, partly_gone, _unrecorded, retiring, retiring_unrecorded = [
        await create_world(db_sessionmaker) for _ in range(7)
    ]
    # As after a Temporal reset: recorded, but unknown to this server.
    await _record(db_sessionmaker, orphaned, "a", "b")
    await _record(db_sessionmaker, partly_gone, "gone")
    await _record(db_sessionmaker, retiring, "gone")
    async with db_sessionmaker() as session, session.begin():
        await session.execute(
            update(World)
            .where(World.id.in_([retiring, retiring_unrecorded]))
            .values(status="retiring")
        )

    async with main_worker(client, db_sessionmaker):
        for world_id in (running, finished, partly_gone):
            handle = await start_world_workflow(
                client,
                db_sessionmaker,
                ParkedWorkflow.run,
                "x",
                world_id=world_id,
                workflow_id=world_workflow_id(world_id, "conv", "live"),
            )
            if world_id == finished:
                await handle.signal(ParkedWorkflow.release)
                await handle.result()

        retirer = _RecordingRetirer()
        retired = await retire_orphaned_worlds(client, db_sessionmaker, retirer)

    assert (
        sorted(retired)
        == sorted(retirer.retired)
        == sorted([orphaned, retiring, retiring_unrecorded])
    )


async def test_a_namespace_that_does_not_exist_retires_nothing(
    temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    """A typo in TEMPORAL_NAMESPACE makes every describe NOT_FOUND; that is not "every
    workflow is gone"."""
    world_id = await create_world(db_sessionmaker)
    await _record(db_sessionmaker, world_id, "a")
    typo = Client(temporal_env.client.service_client, namespace="harmony-typo")
    retirer = _RecordingRetirer()

    assert await retire_orphaned_worlds(typo, db_sessionmaker, retirer) == []
    assert retirer.retired == []
    # The per-world check refuses to read it as "gone", too (e.g. when the namespace
    # could not be described up front).
    with pytest.raises(RPCError):
        await _any_known(typo, [world_workflow_id(world_id, "conv", "a")])


async def test_a_failing_check_is_skipped_not_raised(
    temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    world_id = await create_world(db_sessionmaker)
    await _record(db_sessionmaker, world_id, "gone")

    class _BrokenRetirer:
        async def retire(self, world_id: UUID) -> None:
            raise RuntimeError("temporal unavailable")

    retired = await retire_orphaned_worlds(temporal_env.client, db_sessionmaker, _BrokenRetirer())
    assert retired == []
