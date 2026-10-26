"""`RetireWorldWorkflow`, ID `retire:{world}` (§7).

It carries no `WorldId` search attribute, so its own sweep never lists it. The steps
are separate activities so the row TTL is a durable timer, not a sleeping activity:
mark retired → terminate the world's workflows → wait `row_ttl` → delete the rows.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from harmony.temporal.activities.retire_world import RetireWorldActivities
    from harmony.temporal.policies import main_activity_options

# Retirement must finish, so its steps keep retrying transient failures, but only for
# an hour each: a stuck retirement then fails outright instead of looping forever. The
# world already refuses work by then, and its workflows' run timeouts still apply.
_RETRY = RetryPolicy(maximum_interval=timedelta(minutes=1))
_STEP_DEADLINE = timedelta(hours=1)


def retire_workflow_id(world_id: UUID) -> str:
    return f"retire:{world_id}"


class RetireWorldInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    world_id: UUID
    # How long the retired world's rows stay readable before deletion; 0 deletes at once.
    row_ttl: timedelta = timedelta(0)


class RetireWorldResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    world_found: bool
    # Workflow IDs this retirement terminated (already-closed ones are not listed).
    terminated: list[str]
    rows_deleted: bool


@workflow.defn
class RetireWorldWorkflow:
    @workflow.run
    async def run(self, retire: RetireWorldInput) -> RetireWorldResult:
        found = await workflow.execute_activity_method(
            RetireWorldActivities.mark_retired,
            retire.world_id,
            **main_activity_options(schedule_to_close=_STEP_DEADLINE, retry_policy=_RETRY),
        )
        if not found:
            return RetireWorldResult(world_found=False, terminated=[], rows_deleted=False)

        terminated = await workflow.execute_activity_method(
            RetireWorldActivities.terminate_workflows,
            retire.world_id,
            **main_activity_options(
                start_to_close=timedelta(minutes=10),
                schedule_to_close=_STEP_DEADLINE,
                heartbeat=timedelta(seconds=30),
                retry_policy=_RETRY,
            ),
        )

        if retire.row_ttl > timedelta(0):
            await workflow.sleep(retire.row_ttl)
        await workflow.execute_activity_method(
            RetireWorldActivities.delete_rows,
            retire.world_id,
            **main_activity_options(
                start_to_close=timedelta(minutes=5),
                schedule_to_close=_STEP_DEADLINE,
                retry_policy=_RETRY,
            ),
        )
        return RetireWorldResult(world_found=True, terminated=terminated, rows_deleted=True)
