"""Workflow-test helpers (not collected by pytest)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import workflow
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from harmony.db.models import Event, World, WorldWorkflow
from harmony.temporal.activities.hello import HelloActivities
from harmony.temporal.activities.retire_world import RetireWorldActivities
from harmony.temporal.policies import MAIN_TASK_QUEUE
from harmony.temporal.workflows.hello import HelloWorkflow
from harmony.temporal.workflows.retire_world import RetireWorldWorkflow

type Sessionmaker = async_sessionmaker[AsyncSession]


@workflow.defn(sandboxed=False)
class ParkedWorkflow:
    """Stands in for a long-lived world workflow: runs until released or terminated."""

    def __init__(self) -> None:
        self._released = False

    @workflow.run
    async def run(self, label: str) -> str:
        await workflow.wait_condition(lambda: self._released)
        return label

    @workflow.signal
    def release(self) -> None:
        self._released = True


def main_worker(
    client: Client,
    sessionmaker: Sessionmaker,
    *,
    workflows: Sequence[type] = (),
    activities: Sequence[Callable[..., Any]] = (),
) -> Worker:
    """A `harmony-main` worker with the real P0 workflows and activities, plus extras.

    The retirement sweep settles quickly so tests don't wait a second per list.
    """
    hello = HelloActivities(sessionmaker)
    retire = RetireWorldActivities(client, sessionmaker, settle_interval=timedelta(seconds=0.1))
    return Worker(
        client,
        task_queue=MAIN_TASK_QUEUE,
        workflows=[HelloWorkflow, RetireWorldWorkflow, ParkedWorkflow, *workflows],
        activities=[
            hello.emit_hello_tick,
            retire.mark_retired,
            retire.terminate_workflows,
            retire.delete_rows,
            *activities,
        ],
    )


async def exists_in_temporal(client: Client, workflow_id: str) -> bool:
    try:
        await client.get_workflow_handle(workflow_id).describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            return False
        raise
    return True


async def world_status(sessionmaker: Sessionmaker, world_id: UUID) -> str | None:
    async with sessionmaker() as session:
        return await session.scalar(select(World.status).where(World.id == world_id))


async def count_rows(
    sessionmaker: Sessionmaker, model: type[Event | WorldWorkflow], world_id: UUID
) -> int:
    async with sessionmaker() as session:
        count = await session.scalar(
            select(func.count()).select_from(model).where(model.world_id == world_id)
        )
    return count or 0
