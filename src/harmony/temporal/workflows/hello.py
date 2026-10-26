"""`HelloWorkflow`: proves the worker → activity → outbox → SSE path with no model.

It emits `hello.tick` events through an activity, with a durable timer between them.
Start it with `harmony dev hello --world <id>`.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from harmony.temporal.activities.hello import HelloActivities, HelloTickInput
    from harmony.temporal.policies import main_activity_options


class HelloInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    world_id: UUID
    ticks: int = Field(default=3, ge=1, le=100)
    interval: timedelta = timedelta(seconds=1)


class HelloResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seqs: list[int]


@workflow.defn
class HelloWorkflow:
    @workflow.run
    async def run(self, hello: HelloInput) -> HelloResult:
        workflow_id = workflow.info().workflow_id
        seqs: list[int] = []
        for n in range(1, hello.ticks + 1):
            if n > 1:
                await workflow.sleep(hello.interval)
            seq = await workflow.execute_activity_method(
                HelloActivities.emit_hello_tick,
                HelloTickInput(
                    world_id=hello.world_id, workflow_id=workflow_id, n=n, of=hello.ticks
                ),
                **main_activity_options(),
            )
            seqs.append(seq)
        return HelloResult(seqs=seqs)
