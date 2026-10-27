"""Starting world workflows: the only way the API and the CLI start one (§7).

`start_world_workflow` guarantees, for every world workflow:

- its ID is `w:{world}:...`, so IDs never collide across worlds;
- it carries the typed `WorldId` (and `OrgId`) search attributes, which retirement's
  list sweep relies on;
- it has a 24 h execution timeout, the backstop if retirement ever misses it. It
  spans the whole continue-as-new chain (a run timeout alone restarts with each new
  run), so no world workflow outlives its first start by more than a day;
- it is recorded in `world_workflows`, which retirement terminates by ID and the API's
  startup check reads;
- it is never started in a world that is not `live`.

The liveness check holds `FOR SHARE` on the `worlds` row until the start has returned
and the record is committed. Retirement's status update waits on that lock, so once a
world is marked retired, every workflow started in it is already recorded. (If the
commit itself fails after a successful start, the workflow runs unrecorded; the
list sweep and the activities' status guard still catch it.)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.types import MethodAsyncSingleParam

from harmony.db.models import World, WorldWorkflow
from harmony.errors import WorldRetired
from harmony.temporal.policies import MAIN_TASK_QUEUE
from harmony.temporal.search_attributes import world_search_attributes

WORLD_RUN_TIMEOUT = timedelta(hours=24)


class WorldNotFound(LookupError):
    """No world with this ID."""

    def __init__(self, world_id: UUID) -> None:
        super().__init__(f"world {world_id} not found")
        self.world_id = world_id


def world_workflow_id(world_id: UUID, kind: str, *parts: object) -> str:
    """`w:{world}:{kind}[:{part}...]`, e.g. `w:<world>:thread:<thread_id>` (§7)."""
    return ":".join(["w", str(world_id), kind, *(str(part) for part in parts)])


def workflow_type_name(run: MethodAsyncSingleParam[Any, Any, Any]) -> str:
    """The registered type name of a `@workflow.run` method's workflow.

    Uses the SDK's own resolution, which also honours `@workflow.defn(name=...)` and
    fails fast on a method that is not a workflow's run method.
    """
    name = workflow._Definition.must_from_run_fn(run).name  # pyright: ignore[reportPrivateUsage]
    if name is None:
        raise ValueError("a dynamic workflow cannot be started as a world workflow")
    return name


async def start_world_workflow[S, P, R](
    client: Client,
    sessionmaker: async_sessionmaker[AsyncSession],
    run: MethodAsyncSingleParam[S, P, R],
    arg: P,
    *,
    world_id: UUID,
    workflow_id: str,
    org_id: UUID | None = None,
    task_queue: str = MAIN_TASK_QUEUE,
    id_conflict_policy: WorkflowIDConflictPolicy = WorkflowIDConflictPolicy.FAIL,
    start_signal: str | None = None,
    start_signal_args: Sequence[Any] = (),
    memo: Mapping[str, Any] | None = None,
) -> WorkflowHandle[S, R]:
    """Start `run(arg)` as a workflow of `world_id` and record it.

    Raises `WorldNotFound` for an unknown world and `harmony.errors.WorldRetired` for one
    that is `retiring` or `retired`; nothing is started or recorded then. Pass
    `start_signal` for signal-with-start (entity workflows), usually with
    `id_conflict_policy=USE_EXISTING`. Recording is idempotent, so restarting the same
    ID is fine, and a closed ID starts again (the SDK's default `ALLOW_DUPLICATE`). A
    `memo` is readable through `describe()` (a reopen names its attempt there).
    """
    prefix = f"w:{world_id}:"
    if not workflow_id.startswith(prefix):
        raise ValueError(f"workflow id {workflow_id!r} must start with {prefix!r}")
    workflow_type = workflow_type_name(run)

    async with sessionmaker() as session, session.begin():
        status = (
            await session.execute(
                select(World.status).where(World.id == world_id).with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if status is None:
            raise WorldNotFound(world_id)
        if status != "live":
            raise WorldRetired(world_id)
        await session.execute(
            insert(WorldWorkflow)
            .values(world_id=world_id, workflow_id=workflow_id, workflow_type=workflow_type)
            .on_conflict_do_nothing()
        )
        # Started inside the transaction: if the start fails the record rolls back, and
        # retirement cannot mark the world retired between the check and the start.
        return await client.start_workflow(
            run,
            arg,
            id=workflow_id,
            task_queue=task_queue,
            execution_timeout=WORLD_RUN_TIMEOUT,
            run_timeout=WORLD_RUN_TIMEOUT,
            search_attributes=world_search_attributes(world_id, org_id),
            id_conflict_policy=id_conflict_policy,
            start_signal=start_signal,
            start_signal_args=start_signal_args,
            memo=memo,
        )
