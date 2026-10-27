"""`start_reopen`: a member's Reopen, as `POST /api/threads/{id}/reopen` runs it
(docs/design/REOPEN.md §4.2). Kept out of the route so tests call it without HTTP.

Under the thread's row lock, which serializes reopens of one thread (both sides, two
tabs, a double click), it checks who may reopen and whether an attempt is already
running, records the attempt as a `reopen` steer, and starts the thread's next run
under the same workflow id with `FAIL`, naming the attempt in the run's memo. The
steer is the attempt's only id: a retried POST finds it, and a `working` one whose run
isn't running was abandoned and is settled here. The run itself writes the guidance and
reopens the thread (`ThreadWorkflow`); nothing the other side sees changes before that.

Errors are the route's `ApiProblem`s.

Owner: E4.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client, WorkflowExecutionDescription, WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from harmony.api.routes.acting import require_acting
from harmony.api.routes.problems import (
    ApiProblemError,
    ReopenedBy,
    invalid,
    member_ref,
    not_found,
    problem,
    world_retired,
)
from harmony.auth.access import get_visible_thread
from harmony.auth.session import Actor
from harmony.auth.views import org_ref
from harmony.db import repo
from harmony.db.models import Org, Steer, Thread, ThreadRun
from harmony.db.models.choices import ReopenBlock, Side
from harmony.db.uow import UnitOfWork, unit_of_work
from harmony.domain import reopens, steers, threads
from harmony.errors import WorldRetired
from harmony.temporal.activities.network import REQUEST_LIFETIME, workflow_missing
from harmony.temporal.start import start_world_workflow
from harmony.temporal.workflows.payloads import (
    STEER_BODY_MAX_CHARS,
    ReopenInput,
    ThreadCarry,
    ThreadInput,
    thread_workflow_id,
)
from harmony.temporal.workflows.thread import SIDES, ThreadWorkflow

# A reopened run's deadline, from when it is started: as long as a request's.
REOPEN_LIFETIME: Final = REQUEST_LIFETIME
# The memo key naming the attempt a run was started for.
MEMO_STEER: Final = "reopen_steer_id"
# A start refused because the last run is in its final workflow task (after
# `finish_run`) is tried again this many times, this far apart.
_START_RETRIES: Final = 5
_START_RETRY_WAIT_S: Final = 0.3


class _RunEnding(Exception):
    """The last run hasn't returned yet: try the whole reopen again shortly."""


async def start_reopen(
    client: Client,
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: Actor,
    thread_id: UUID,
    *,
    client_key: UUID,
    as_member_id: UUID,
    body: str,
) -> UUID:
    """Start reopening the thread from the actor's side (module docstring). Returns the
    attempt's steer id, the same for a retried POST."""
    await require_acting(sessionmaker, actor, as_member_id)
    text = body.strip()
    if not 1 <= len(text) <= STEER_BODY_MAX_CHARS:
        raise invalid(f"say what Claude should pick up, in 1 to {STEER_BODY_MAX_CHARS} characters")
    steer_id = steers.steer_id_for(actor.world_id, actor.org_id, actor.member_id, client_key)
    for attempt in range(_START_RETRIES + 1):
        try:
            return await _reopen(client, sessionmaker, actor, thread_id, steer_id, text)
        except _RunEnding:
            if attempt < _START_RETRIES:
                await asyncio.sleep(_START_RETRY_WAIT_S)
        except WorldRetired:
            raise world_retired() from None
    raise _closing_in_progress()


async def _reopen(
    client: Client,
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: Actor,
    thread_id: UUID,
    steer_id: UUID,
    body: str,
) -> UUID:
    async with unit_of_work(sessionmaker, actor.world_id) as uow:
        session = uow.session
        try:
            await get_visible_thread(session, actor, thread_id)
        except repo.NotFoundInWorld:
            raise not_found("thread") from None
        thread = await threads.locked(uow, thread_id)
        try:
            side = threads.side_of(thread, actor.org_id)
        except ValueError:
            raise problem(
                status.HTTP_403_FORBIDDEN,
                "not_in_org",
                "your organization isn't a party to this conversation",
            ) from None
        existing = await repo.get(session, Steer, actor.world_id, steer_id)
        if existing is not None:
            if (existing.thread_id, existing.kind, existing.body) != (thread_id, "reopen", body):
                raise invalid("this reopen was already sent with different text")
            return existing.id
        runs = await threads.runs(session, actor.world_id, thread_id)
        current = next((row for row in runs if row.run == thread.run), None)
        if thread.state not in threads.TERMINAL_STATES:
            return await _already_open(session, actor, current, body)
        running = await _running_attempt(client, uow, actor, thread_id, body)
        if running is not None:
            await uow.commit()
            return running
        reopen_offer = reopens.reopen_offer(
            thread,
            runs,
            side,
            requester_wrote=await reopens.requester_wrote(session, thread),
            now=datetime.now(UTC),
        )
        if reopen_offer is not None and reopen_offer.state == "blocked":
            raise _not_reopenable(reopen_offer.block)
        if thread.run_finished_at is None or current is None or current.carry is None:
            described = await _describe(client, actor.world_id, thread_id)
            if described is not None and described.status == WorkflowExecutionStatus.RUNNING:
                raise _closing_in_progress()
            raise _not_reopenable("unfinished")
        steer = await steers.record(
            uow,
            steer_id=steer_id,
            thread_id=thread_id,
            org_id=actor.org_id,
            kind="reopen",
            author_member_id=actor.member_id,
            body=body,
        )
        arg = await _thread_input(session, thread, current, steer, side)
        try:
            await start_world_workflow(
                client,
                sessionmaker,
                ThreadWorkflow.run,
                arg,
                world_id=actor.world_id,
                workflow_id=thread_workflow_id(actor.world_id, thread_id),
                org_id=thread.requester_org_id,
                memo={MEMO_STEER: str(steer.id)},
            )
        except WorkflowAlreadyStartedError:
            started_for = await _attempt_of(await _describe(client, actor.world_id, thread_id))
            if started_for == steer.id:
                # An identical POST started it, then failed to commit.
                await uow.commit()
                return steer.id
            if started_for is not None and started_for != current.steer_id:
                other = await repo.get(session, Steer, actor.world_id, started_for)
                raise await _in_progress(session, actor, other) from None
            raise _RunEnding from None
        await uow.commit()
        return steer.id


async def _already_open(
    session: AsyncSession, actor: Actor, current: ThreadRun | None, body: str
) -> UUID:
    """The thread is live: the member's own reopen of it is this one again; otherwise
    409 `thread_open` naming who reopened it (nobody, for a thread never closed)."""
    if current is None or current.steer_id is None or current.reopened_by_org_id is None:
        raise problem(status.HTTP_409_CONFLICT, "thread_open", "this conversation is open")
    steer = await repo.get_required(session, Steer, actor.world_id, current.steer_id)
    if steer.author_member_id == actor.member_id and steer.body == body:
        return steer.id
    org = await repo.get_required(session, Org, actor.world_id, current.reopened_by_org_id)
    member = (
        await member_ref(session, actor.world_id, steer.author_member_id)
        if org.id == actor.org_id
        else None
    )
    raise problem(
        status.HTTP_409_CONFLICT,
        "thread_open",
        "this conversation was reopened",
        reopened_by=ReopenedBy(org=org_ref(org), member=member),
    )


async def _running_attempt(
    client: Client, uow: UnitOfWork, actor: Actor, thread_id: UUID, body: str
) -> UUID | None:
    """An attempt still running: the member's own with the same text is this one again
    (its id), anyone else's is 409 `reopen_in_progress`. One whose run isn't running was
    abandoned: it is settled `reopen_failed`, and the reopen goes on (None)."""
    session = uow.session
    working = list(
        await session.scalars(
            repo.in_world(Steer, actor.world_id).where(
                Steer.thread_id == thread_id, Steer.kind == "reopen", Steer.status == "working"
            )
        )
    )
    if not working:
        return None
    described = await _describe(client, actor.world_id, thread_id)
    running = (
        await _attempt_of(described)
        if described is not None and described.status == WorkflowExecutionStatus.RUNNING
        else None
    )
    for steer in working:
        if steer.id != running:
            await steers.settle(uow, steer.id, failure="reopen_failed")
            continue
        if steer.author_member_id == actor.member_id and steer.body == body:
            return steer.id
        raise await _in_progress(session, actor, steer)
    return None


async def _in_progress(session: AsyncSession, actor: Actor, steer: Steer | None) -> ApiProblemError:
    """409 `reopen_in_progress`: who is reopening it only for the actor's own org, since
    an attempt is private until it lands."""
    reopened_by = None
    if steer is not None and steer.org_id == actor.org_id:
        org = await repo.get_required(session, Org, actor.world_id, steer.org_id)
        reopened_by = ReopenedBy(
            org=org_ref(org),
            member=await member_ref(session, actor.world_id, steer.author_member_id),
        )
    return problem(
        status.HTTP_409_CONFLICT,
        "reopen_in_progress",
        "this conversation is being reopened",
        reopened_by=reopened_by,
    )


async def _thread_input(
    session: AsyncSession, thread: Thread, current: ThreadRun, steer: Steer, side: Side
) -> ThreadInput:
    """The next run's input: what the last run carried, fresh turns and deadline, and
    each side's guidance and steer watermark, this steer's included."""
    carry = ThreadCarry.model_validate(current.carry)
    world_id = thread.world_id
    guidance: dict[Side, int] = {}
    steer_seq: dict[Side, int] = {}
    for each in SIDES:
        latest = await steers.latest_guidance(session, world_id, thread.id, each)
        if latest is not None:
            guidance[each] = latest.version
        steer_seq[each] = await steers.max_seq(session, world_id, thread.id, each)
    return ThreadInput(
        world_id=world_id,
        thread_id=thread.id,
        request_id=thread.request_id,
        requester_org_id=thread.requester_org_id,
        responder_org_id=thread.responder_org_id,
        cassette_thread=await threads.cassette_key(session, thread),
        max_turns=sum(carry.turns.values()) + threads.MAX_TURNS,
        deadline_at=datetime.now(UTC) + REOPEN_LIFETIME,
        run=thread.run + 1,
        carry=carry,
        reopen=ReopenInput(
            steer_id=steer.id,
            side=side,
            org_id=steer.org_id,
            member_id=steer.author_member_id,
            body=steer.body,
            seq=steer.seq,
            from_state=thread.state,
        ),
        guidance=guidance,
        steer_seq=steer_seq,
    )


async def _describe(
    client: Client, world_id: UUID, thread_id: UUID
) -> WorkflowExecutionDescription | None:
    """The thread's latest run, or None when it never ran (a seeded thread)."""
    try:
        return await client.get_workflow_handle(thread_workflow_id(world_id, thread_id)).describe()
    except RPCError as error:
        if workflow_missing(error):
            return None
        raise


async def _attempt_of(described: WorkflowExecutionDescription | None) -> UUID | None:
    """The reopen attempt a run was started for, from its memo; None for a first run."""
    if described is None:
        return None
    value = await described.memo_value(MEMO_STEER, None)
    return UUID(value) if isinstance(value, str) else None


def _not_reopenable(block: ReopenBlock | None) -> ApiProblemError:
    return problem(
        status.HTTP_409_CONFLICT,
        "not_reopenable",
        "this conversation can't be reopened",
        block=block,
    )


def _closing_in_progress() -> ApiProblemError:
    return problem(
        status.HTTP_409_CONFLICT,
        "closing_in_progress",
        "Claude is still wrapping up this conversation",
    )


__all__ = ["MEMO_STEER", "REOPEN_LIFETIME", "start_reopen"]
