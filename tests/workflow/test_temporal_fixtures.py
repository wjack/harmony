"""`temporal_env` runs the local dev server with the WorldId/OrgId search attributes."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from temporalio import workflow
from temporalio.common import SearchAttributePair, TypedSearchAttributes
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from harmony.temporal.search_attributes import ORG_ID, WORLD_ID


@workflow.defn(sandboxed=False)
class _Wait:
    def __init__(self) -> None:
        self._done = False

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._done)

    @workflow.signal
    def finish(self) -> None:
        self._done = True


async def test_listing_finds_workflows_by_world(temporal_env: WorkflowEnvironment) -> None:
    client = temporal_env.client
    world, other = str(uuid4()), str(uuid4())
    queue = f"q-{uuid4()}"
    async with Worker(client, task_queue=queue, workflows=[_Wait]):
        handles = [
            await client.start_workflow(
                _Wait.run,
                id=f"w:{w}:probe",
                task_queue=queue,
                search_attributes=TypedSearchAttributes(
                    [SearchAttributePair(WORLD_ID, w), SearchAttributePair(ORG_ID, "henderson")]
                ),
            )
            for w in (world, other)
        ]
        query = f"WorldId = '{world}' AND ExecutionStatus = 'Running'"
        ids: list[str] = []
        for _ in range(50):
            ids = [wf.id async for wf in client.list_workflows(query)]
            if ids:
                break
            await asyncio.sleep(0.1)
        assert ids == [f"w:{world}:probe"]
        for handle in handles:
            await handle.signal(_Wait.finish)
            await handle.result()
