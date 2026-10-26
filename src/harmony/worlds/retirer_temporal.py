"""The production `Retirer`: hands retirement to `RetireWorldWorkflow` (§7, §11.1).

`FallbackRetirer` is what the API runs: the Temporal retirer, falling back to
retiring inline while Temporal is unreachable.

Also the API-startup check (§13): after a Temporal reset, live worlds point at
workflows that no longer exist; `retire_orphaned_worlds` finds and retires them, and
finishes every retirement that was left `retiring`.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError, RPCStatusCode

from harmony.db.models import World, WorldWorkflow
from harmony.temporal.policies import MAIN_TASK_QUEUE
from harmony.temporal.workflows.retire_world import (
    RetireWorldInput,
    RetireWorldWorkflow,
    retire_workflow_id,
)
from harmony.worlds.retirer import Retirer

logger = logging.getLogger(__name__)

# A retired desk world stays readable this long (the visitor may still have it open)
# before its rows are deleted.
DEFAULT_ROW_TTL = timedelta(hours=1)
# The gRPC detail Temporal attaches to a NOT_FOUND about the namespace itself.
_NAMESPACE_NOT_FOUND = "temporal.api.errordetails.v1.NamespaceNotFoundFailure"


class TemporalRetirer:
    """Marks the world `retiring` at once, then starts `RetireWorldWorkflow`.

    Returns as soon as the workflow is started; the visitor never waits on
    termination. Retiring again (or concurrently) is safe: a running retirement is
    reused, and a finished one re-runs as a no-op.

    Call it after committing any transaction that changed the world's row (such as
    `fork_and_reset` marking it `retiring`): its own update would wait on that lock.
    """

    def __init__(
        self,
        client: Client,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        row_ttl: timedelta = DEFAULT_ROW_TTL,
        task_queue: str = MAIN_TASK_QUEUE,
    ) -> None:
        self._client = client
        self._sessionmaker = sessionmaker
        self._row_ttl = row_ttl
        self._task_queue = task_queue

    async def retire(self, world_id: UUID) -> None:
        # Commits and activities refuse a non-live world, so from here on nothing new
        # lands in it, even before the workflow's first activity runs.
        async with self._sessionmaker() as session, session.begin():
            await session.execute(
                update(World)
                .where(World.id == world_id, World.status == "live")
                .values(status="retiring")
            )
            exists = await session.scalar(select(World.id).where(World.id == world_id))
        if exists is None:
            return
        await self._client.start_workflow(
            RetireWorldWorkflow.run,
            RetireWorldInput(world_id=world_id, row_ttl=self._row_ttl),
            id=retire_workflow_id(world_id),
            task_queue=self._task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )


def temporal_unreachable(exc: BaseException) -> bool:
    """Whether `exc` means the Temporal server could not be reached at all.

    A lazy client that never connected raises `RuntimeError("Failed client connect:
    ...")` on its first call; a connected one whose server went away raises
    `RPCError(UNAVAILABLE)`.
    """
    if isinstance(exc, RPCError):
        return exc.status in (RPCStatusCode.UNAVAILABLE, RPCStatusCode.DEADLINE_EXCEEDED)
    return isinstance(exc, RuntimeError) and str(exc).startswith("Failed client connect")


class FallbackRetirer:
    """The API's retirer: Temporal when it is reachable, inline otherwise.

    With Temporal down there is nothing to terminate, and any workflow of the world
    that survives fails at its next status check, so retiring inline (`SyncRetirer`)
    loses nothing; leaving the world `retiring` instead would keep its rows until the
    next API startup (`retire_orphaned_worlds`) or wipe. Any other failure propagates.
    """

    def __init__(self, primary: Retirer, fallback: Retirer) -> None:
        self._primary = primary
        self._fallback = fallback

    async def retire(self, world_id: UUID) -> None:
        try:
            await self._primary.retire(world_id)
        except Exception as exc:
            if not temporal_unreachable(exc):
                raise
            logger.warning("Temporal is unreachable; retiring world %s inline: %s", world_id, exc)
            await self._fallback.retire(world_id)


async def retire_orphaned_worlds(
    client: Client,
    sessionmaker: async_sessionmaker[AsyncSession],
    retirer: Retirer,
    *,
    concurrency: int = 8,
) -> list[UUID]:
    """Retire every `live` world that has workflows on record, none of which Temporal
    knows any more, and finish every `retiring` world's retirement. Returns the IDs
    of the worlds handed to `retirer`.

    A world whose workflows finished normally still has them in Temporal (until the
    namespace's retention passes) and is left alone; so is a world with no recorded
    workflows. Each check is a strongly consistent `describe`, never a list query
    over search attributes, which could miss a workflow started a moment ago.

    A world is left `retiring` when its retirement never started: the process died
    between committing `retiring` and starting `RetireWorldWorkflow`, or the start
    failed with an error the fallback does not cover. `Retirer.retire` is idempotent
    (a retirement still under way is joined), so each is simply retired again.

    When the client's namespace does not exist (a misconfigured `TEMPORAL_NAMESPACE`)
    nothing is retired: every describe would be NOT_FOUND, which must not read as
    "every workflow is gone".

    Call it once at API startup, in the background. A world whose check fails (say,
    Temporal is unreachable) is logged and skipped, so it is checked again at the
    next startup.
    """
    if not await _namespace_exists(client):
        return []
    async with sessionmaker() as session:
        retiring = list(
            await session.scalars(
                select(World.id).where(World.status == "retiring").order_by(World.created_at)
            )
        )
        rows = (
            await session.execute(
                select(WorldWorkflow.world_id, WorldWorkflow.workflow_id)
                .join(World, World.id == WorldWorkflow.world_id)
                .where(World.status == "live")
                # Newest first: a recent workflow is the likeliest to still exist.
                .order_by(WorldWorkflow.world_id, WorldWorkflow.created_at.desc())
            )
        ).all()
    recorded: defaultdict[UUID, list[str]] = defaultdict(list)
    for world_id, workflow_id in rows:
        recorded[world_id].append(workflow_id)

    limit = asyncio.Semaphore(concurrency)

    async def finish(world_id: UUID) -> UUID | None:
        async with limit:
            try:
                logger.info("finishing the retirement of world %s, left retiring", world_id)
                await retirer.retire(world_id)
            except Exception:
                logger.warning("retiring world %s failed again", world_id, exc_info=True)
                return None
            return world_id

    async def check(world_id: UUID, workflow_ids: list[str]) -> UUID | None:
        async with limit:
            try:
                if await _any_known(client, workflow_ids):
                    return None
                logger.info(
                    "retiring world %s: none of its %d workflows exist",
                    world_id,
                    len(workflow_ids),
                )
                await retirer.retire(world_id)
            except Exception:
                logger.warning("orphan check failed for world %s", world_id, exc_info=True)
                return None
            return world_id

    results = await asyncio.gather(
        *(finish(world_id) for world_id in retiring),
        *(check(w, ids) for w, ids in recorded.items()),
    )
    return [world_id for world_id in results if world_id is not None]


async def _namespace_exists(client: Client) -> bool:
    try:
        await client.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=client.namespace)
        )
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            logger.error(
                "skipping the orphan check: Temporal has no namespace %r", client.namespace
            )
            return False
        # Unreachable, or not allowed to describe it: each check below decides.
        logger.warning("could not describe namespace %r: %s", client.namespace, err)
    return True


def _namespace_missing(err: RPCError) -> bool:
    return any(detail.type_url.endswith(_NAMESPACE_NOT_FOUND) for detail in err.grpc_status.details)


async def _any_known(client: Client, workflow_ids: list[str]) -> bool:
    for workflow_id in workflow_ids:
        try:
            await client.get_workflow_handle(workflow_id).describe()
        except RPCError as err:
            # Only "no such workflow" means gone; "no such namespace" says nothing.
            if err.status != RPCStatusCode.NOT_FOUND or _namespace_missing(err):
                raise
        else:
            return True
    return False
