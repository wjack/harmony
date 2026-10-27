"""Reopen: another run of an ended thread, started by a member of either party
(docs/design/REOPEN.md).

`reopen_offer` says whether the viewer's side may reopen a thread now (§3.2), for the
thread read and the reopen route alike. A reopened run first writes its side's guidance with
the thread still ended; only then does `begin` reopen it, in one transaction (§4.4).
Every run ends with `finish_run`, which stores how it ended and what the next run
carries (§4.6). `end_line` is the code-written reason the guidance writer is told the
conversation closed for (EDIT.md §3.9).

Owner: E4.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents import context
from harmony.db import repo
from harmony.db.models import Org, RepSession, Steer, Thread, ThreadMessage, ThreadRun
from harmony.db.models.base import JsonObject
from harmony.db.models.choices import EndReason, FailureReason, ReopenBlock, Side
from harmony.db.uow import UnitOfWork
from harmony.domain import requests, steers, threads
from harmony.domain.errors import ReopenConflict

# A conversation is reopened at most this many times (D3).
MAX_REOPENS: Final = 2
# How long Reopen waits for a run's wrap-up (its last summary turn) before it lets the
# reopen route decide from the workflow itself.
WRAP_UP_GRACE: Final = timedelta(minutes=10)

ReopenOfferState = Literal["allowed", "wrapping_up", "blocked"]


@dataclass(frozen=True, slots=True)
class ReopenOffer:
    state: ReopenOfferState
    block: ReopenBlock | None = None


def reopen_offer(
    thread: Thread,
    runs: Sequence[ThreadRun],
    side: Side,
    *,
    requester_wrote: bool,
    now: datetime,
) -> ReopenOffer | None:
    """Whether `side` may reopen the thread now (REOPEN.md §3.2, checked in its order);
    None while the thread is open. `requester_wrote`: the requester has sent a message
    on it, in any run."""
    if thread.state not in threads.TERMINAL_STATES:
        return None
    if not runs:
        return ReopenOffer("blocked", "history")
    if (
        thread.run_finished_at is None
        and thread.ended_at is not None
        and now - thread.ended_at < WRAP_UP_GRACE
    ):
        return ReopenOffer("wrapping_up")
    if thread.run - 1 >= MAX_REOPENS:
        return ReopenOffer("blocked", "limit")
    them = threads.other(side)
    if (
        thread.end_reason == "stopped"
        and thread.stop_reason == "refused"
        and thread.closed_by == them
    ):
        return ReopenOffer("blocked", "refused")
    current = next((row for row in runs if row.run == thread.run), None)
    if (
        current is not None
        and current.reopened_side == side
        and thread.end_reason == "closed"
        and thread.closed_by == them
    ):
        return ReopenOffer("blocked", "they_closed")
    if side == "responder" and not requester_wrote:
        return ReopenOffer("blocked", "not_asked")
    return ReopenOffer("allowed")


async def requester_wrote(session: AsyncSession, thread: Thread) -> bool:
    """Whether the requester has sent a message on the thread, in any run."""
    found = await session.scalar(
        select(ThreadMessage.id)
        .where(
            ThreadMessage.world_id == thread.world_id,
            ThreadMessage.thread_id == thread.id,
            ThreadMessage.from_org_id == thread.requester_org_id,
            ThreadMessage.kind == "message",
        )
        .limit(1)
    )
    return found is not None


_STOPPED: Final[dict[FailureReason, str]] = {
    "model_error": "the model service returned an error",
    "paused": "live models were paused",
    "session_budget": "a live-model budget ran out",
    "daily_budget": "a live-model budget ran out",
    "demo_budget": "a live-model budget ran out",
}


def end_line(
    end_reason: EndReason | None,
    *,
    side: Side,
    closed_by: Side | None,
    stop_reason: FailureReason | None,
    counterparty: str,
) -> str:
    """Why the conversation closed, as `side`'s guidance writer is told it (REOPEN.md
    §2.1): code-written from the thread row, never a model's or member's words."""
    closer = (
        None if closed_by is None else "your representative" if closed_by == side else counterparty
    )
    match end_reason:
        case "closed":
            return f"{closer} closed it" if closer else "it was closed"
        case "quiet":
            return "both sides went quiet"
        case "unsent":
            return "nothing was sent"
        case "turn_limit":
            return "it reached its turn limit"
        case "deadline":
            return "it reached its deadline"
        case "stopped" if stop_reason == "refused":
            return f"{closer or 'a representative'} declined to continue"
        case "stopped":
            return _STOPPED.get(stop_reason, "it stopped") if stop_reason else "it stopped"
        case None:
            return "it ended"


async def begin(
    uow: UnitOfWork,
    *,
    thread_id: UUID,
    run: int,
    steer_id: UUID,
    deadline_at: datetime | None,
    max_turns: int,
) -> int:
    """Reopen the thread as run `run`, from its reopen steer's side (REOPEN.md §4.4):
    the run's row, each side's close summary archived to the last run, the thread live
    again, stale `closed` and `reopened` notices dropped and a `reopened` one queued per
    side, the steer `done`, and the request reopened if it had closed. Returns the
    run's `start_seq`. Idempotent: once the thread is at `run`, writes nothing."""
    session = uow.session
    thread = await threads.locked(uow, thread_id)
    if thread.run >= run:
        started = await session.get(ThreadRun, (uow.world_id, thread_id, run))
        return started.start_seq if started is not None else 0
    if thread.run != run - 1 or thread.state not in threads.TERMINAL_STATES:
        raise ReopenConflict(thread_id, f"it is {thread.state} at run {thread.run}")
    if thread.run_finished_at is None:
        raise ReopenConflict(thread_id, "its last run hasn't finished")
    steer = await repo.get_required(session, Steer, uow.world_id, steer_id)
    if steer.kind != "reopen" or steer.thread_id != thread_id or steer.status != "working":
        raise ReopenConflict(thread_id, f"steer {steer_id} isn't a working reopen of it")
    side = steer.side
    start_seq = await threads.last_seq(session, uow.world_id, thread_id)
    session.add(
        ThreadRun(
            world_id=uow.world_id,
            thread_id=thread_id,
            run=run,
            started_at=datetime.now(UTC),
            deadline_at=deadline_at,
            max_turns=max_turns,
            reopened_by_member_id=steer.author_member_id,
            reopened_by_org_id=steer.org_id,
            reopened_side=side,
            steer_id=steer.id,
            start_seq=start_seq,
        )
    )
    await _archive_summaries(uow, thread_id, run - 1)
    await threads.reopen(uow, thread_id, run=run, side_to_move=side, deadline_at=deadline_at)
    names = await _names(session, thread)
    for each in ("requester", "responder"):
        await context.drop_undelivered(uow, thread_id=thread_id, side=each)
        await context.queue_notice(
            uow,
            thread_id=thread_id,
            side=each,
            kind="reopened",
            text=context.reopened_notice(names[threads.other(each)], by_us=each == side),
        )
    steer.status = "done"
    steers.snapshot(uow, steer)
    await requests.reopen(uow, thread.request_id)
    await session.flush()
    return start_seq


async def finish_run(uow: UnitOfWork, thread_id: UUID, run: int, carry: JsonObject) -> None:
    """The run's last write (REOPEN.md §4.6): how it ended and what the next run
    carries, on its row, then `run_finished_at` on the thread, which lets Reopen on.
    Idempotent."""
    session = uow.session
    thread = await threads.locked(uow, thread_id)
    if thread.run != run or thread.run_finished_at is not None:
        return
    if thread.state not in threads.TERMINAL_STATES:
        raise ValueError(f"thread {thread_id} is {thread.state}: run {run} hasn't ended")
    now = datetime.now(UTC)
    row = await session.get(
        ThreadRun, (uow.world_id, thread_id, run), with_for_update=True, populate_existing=True
    )
    if row is not None:
        row.state = thread.state
        row.end_reason = thread.end_reason
        row.closed_by = thread.closed_by
        row.stop_reason = thread.stop_reason
        row.ended_at = thread.ended_at
        row.carry = carry
        row.finished_at = now
    thread.run_finished_at = now
    await session.flush()
    threads.snapshot(uow, thread)


async def _archive_summaries(uow: UnitOfWork, thread_id: UUID, run: int) -> None:
    """Move each side's close summary to run `run`'s row, so the next run's `close`
    writes its own. A side that never took a turn has no session, and nothing to move."""
    session = uow.session
    ended = await session.get(
        ThreadRun, (uow.world_id, thread_id, run), with_for_update=True, populate_existing=True
    )
    for side in ("requester", "responder"):
        rep = await session.get(
            RepSession,
            (uow.world_id, thread_id, side),
            with_for_update=True,
            populate_existing=True,
        )
        if rep is None:
            continue
        if ended is not None:
            if side == "requester":
                ended.requester_summary = rep.close_summary
            else:
                ended.responder_summary = rep.close_summary
        rep.close_summary = None
    await session.flush()


async def _names(session: AsyncSession, thread: Thread) -> dict[Side, str]:
    orgs = {
        org.id: org.name
        for org in await repo.list_in_world(
            session, Org, thread.world_id, Org.id.in_(threads.parties(thread))
        )
    }
    return {
        "requester": orgs[thread.requester_org_id],
        "responder": orgs[thread.responder_org_id],
    }


__all__ = [
    "MAX_REOPENS",
    "WRAP_UP_GRACE",
    "ReopenOffer",
    "ReopenOfferState",
    "begin",
    "end_line",
    "finish_run",
    "reopen_offer",
    "requester_wrote",
]
