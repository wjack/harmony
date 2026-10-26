"""The steps of retiring a world (§7), as plain functions and as activities.

1. `mark_world_retired`: status `retired`, `retired_at`, and the `world.retired` event,
   in one UnitOfWork. From then on every activity and commit in the world refuses.
2. `terminate_world_workflows`: terminate the recorded root workflows by ID, then
   sweep `WorldId = x AND ExecutionStatus = 'Running'` until two consecutive lists come
   back empty. One workflow at a time: Temporal Cloud allows one batch job per
   namespace, so batch termination is never used.
3. `delete_retired_world_rows`: delete the world's rows, keeping its `worlds` row as a
   tombstone.

The plain functions take no Temporal activity context, so the Temporal-less
`SyncRetirer` can share steps 1 and 3.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from harmony.db import repo
from harmony.db.models import World, WorldWorkflow
from harmony.db.uow import unit_of_work
from harmony.events.schemas import EventScope
from harmony.events.schemas import WorldRetired as WorldRetiredEvent
from harmony.temporal.search_attributes import running_in_world_query

# Workflow listings are eventually consistent: the sweep waits this long between lists, so the
# second empty list is a real second look.
DEFAULT_SETTLE_INTERVAL = timedelta(seconds=1)


def _no_heartbeat() -> None:
    pass


async def mark_world_retired(
    sessionmaker: async_sessionmaker[AsyncSession], world_id: UUID
) -> bool:
    """Mark the world retired and emit `world.retired`; False if there is no such world.

    Idempotent: an already retired world emits nothing.
    """
    async with unit_of_work(sessionmaker, world_id, require_live=False) as uow:
        world = await uow.session.get(World, world_id, with_for_update=True)
        if world is None:
            return False
        if world.status == "retired":
            return True
        world.status = "retired"
        world.retired_at = datetime.now(UTC)
        uow.add_event(WorldRetiredEvent(), EventScope.world())
        await uow.commit()
    return True


async def terminate_world_workflows(
    client: Client,
    sessionmaker: async_sessionmaker[AsyncSession],
    world_id: UUID,
    *,
    reason: str,
    heartbeat: Callable[[], None] = _no_heartbeat,
    settle_interval: timedelta = DEFAULT_SETTLE_INTERVAL,
) -> list[str]:
    """Terminate every workflow of the world; returns the IDs this call terminated.

    A workflow that is already closed or gone counts as done (NotFound is success).
    """
    terminated: list[str] = []

    async def terminate(workflow_id: str, run_id: str | None = None) -> None:
        try:
            await client.get_workflow_handle(workflow_id, run_id=run_id).terminate(reason=reason)
        except RPCError as err:
            if err.status != RPCStatusCode.NOT_FOUND:
                raise
        else:
            if workflow_id not in terminated:
                terminated.append(workflow_id)
        heartbeat()

    async with sessionmaker() as session:
        recorded = (
            await session.scalars(
                select(WorldWorkflow.workflow_id)
                .where(WorldWorkflow.world_id == world_id)
                .order_by(WorldWorkflow.created_at)
            )
        ).all()
    for workflow_id in recorded:
        await terminate(workflow_id)

    query = running_in_world_query(world_id)
    empty_lists = 0
    while empty_lists < 2:
        running = [(wf.id, wf.run_id) async for wf in client.list_workflows(query)]
        heartbeat()
        if running:
            empty_lists = 0
            for workflow_id, run_id in running:
                await terminate(workflow_id, run_id)
        else:
            empty_lists += 1
        if empty_lists < 2:
            await asyncio.sleep(settle_interval.total_seconds())
    return terminated


async def delete_retired_world_rows(
    sessionmaker: async_sessionmaker[AsyncSession], world_id: UUID
) -> None:
    """Delete the world's rows; raises `ValueError` unless it is retired."""
    async with sessionmaker() as session, session.begin():
        await repo.delete_world_rows(session, world_id)


class RetireWorldActivities:
    """The steps as activities on `harmony-main`. Names are explicit so a refactor
    never renames an activity under a running workflow's history."""

    def __init__(
        self,
        client: Client,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        settle_interval: timedelta = DEFAULT_SETTLE_INTERVAL,
    ) -> None:
        self._client = client
        self._sessionmaker = sessionmaker
        self._settle_interval = settle_interval

    @activity.defn(name="retire_world.mark_retired")
    async def mark_retired(self, world_id: UUID) -> bool:
        return await mark_world_retired(self._sessionmaker, world_id)

    @activity.defn(name="retire_world.terminate_workflows")
    async def terminate_workflows(self, world_id: UUID) -> list[str]:
        return await terminate_world_workflows(
            self._client,
            self._sessionmaker,
            world_id,
            reason=f"world {world_id} retired",
            heartbeat=activity.heartbeat,
            settle_interval=self._settle_interval,
        )

    @activity.defn(name="retire_world.delete_rows")
    async def delete_rows(self, world_id: UUID) -> None:
        await delete_retired_world_rows(self._sessionmaker, world_id)
