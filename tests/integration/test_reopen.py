"""Reopen over Postgres (docs/design/REOPEN.md §10.2): how a run ends and the next one
begins, a request's rounds, and `POST /threads/{id}/reopen` against a Temporal dev
server with no worker, so an attempt stays running until a test opens the thread the
way `begin_reopen` would.

What these guard is privacy as much as state: a reopen's text and who sent it stay
with the reopening side, the counterparty learns only that its counterparty reopened,
and each side's earlier summaries stay its own."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment

from harmony.agents import context
from harmony.api.app import create_app
from harmony.config import Settings
from harmony.db import repo
from harmony.db.models import (
    Event,
    NetworkRequest,
    RepNotice,
    RepSession,
    Steer,
    Thread,
    ThreadRun,
)
from harmony.db.uow import unit_of_work
from harmony.domain import reopens, requests, steers, threads
from harmony.temporal.reopen import MEMO_STEER
from harmony.temporal.workflows.payloads import (
    ReopenInput,
    ThreadCarry,
    ThreadInput,
    thread_workflow_id,
)
from tests.integration._e4_world import (
    ALEX,
    CALDER,
    HENDERSON,
    NORTHLAKE,
    approved_outreach,
    open_thread,
    seeded_world,
    sid,
)
from tests.integration._visitors import ADMIN_SECRET, Visitor, visitor

type Sessionmaker = async_sessionmaker[AsyncSession]
type Json = dict[str, Any]

HENDERSON_NAME: Final = "Henderson Frame Lab"
NORTHLAKE_NAME: Final = "Northlake Characterization Core"
REOPEN_TEXT: Final = "Ask whether Nov 5 works instead."
SUMMARY: Final = "Northlake can run it on Nov 4."
# What run 1 hands run 2: three turns taken, nothing owed.
CARRY: Final = ThreadCarry(
    turns={"requester": 2, "responder": 1},
    delivered={"requester": 2, "responder": 1},
    consults={},
    owed={},
)


@dataclass(frozen=True)
class Ended:
    world_id: UUID
    request_id: UUID
    thread_id: UUID


async def _ended(sm: Sessionmaker, world_id: UUID, *, finished: bool = True) -> Ended:
    """Alex's request to Northlake, one exchange, closed by Henderson's representative
    with its summary (Northlake's side never stored one), and the request closed with
    it. `finished`: run 1 wrote its carry."""
    outreach = await approved_outreach(sm, world_id, NORTHLAKE)
    thread_id = await open_thread(sm, outreach, NORTHLAKE)
    async with unit_of_work(sm, world_id) as uow:
        for org_id, body in ((HENDERSON, "Can you run XRD by Nov 6?"), (NORTHLAKE, "Yes: Nov 4.")):
            await threads.append_message(
                uow,
                thread_id,
                message_id=uuid4(),
                from_org_id=org_id,
                kind="message",
                body=body,
                provenance="agent",
            )
        await threads.set_state(
            uow, thread_id, "closed", end_reason="closed", closed_by="requester"
        )
        uow.session.add(
            RepSession(
                world_id=world_id,
                thread_id=thread_id,
                side="requester",
                system_text="",
                tools_text="",
                model="test",
                close_summary=SUMMARY,
            )
        )
        if finished:
            await reopens.finish_run(uow, thread_id, 1, CARRY.model_dump(mode="json"))
        await requests.end_round(uow, outreach.request_id, from_workflow=True)
        await uow.commit()
    return Ended(world_id, outreach.request_id, thread_id)


async def _reopen_steer(sm: Sessionmaker, ended: Ended) -> UUID:
    async with unit_of_work(sm, ended.world_id) as uow:
        steer = await steers.record(
            uow,
            steer_id=uuid4(),
            thread_id=ended.thread_id,
            org_id=HENDERSON,
            kind="reopen",
            author_member_id=ALEX,
            body=REOPEN_TEXT,
        )
        await uow.commit()
    return steer.id


async def _begin(sm: Sessionmaker, ended: Ended, steer_id: UUID) -> int:
    async with unit_of_work(sm, ended.world_id) as uow:
        start_seq = await reopens.begin(
            uow,
            thread_id=ended.thread_id,
            run=2,
            steer_id=steer_id,
            deadline_at=None,
            max_turns=19,
        )
        await uow.commit()
    return start_seq


async def _notices(sm: Sessionmaker, ended: Ended) -> list[tuple[str, str, str]]:
    async with sm() as session:
        rows = await session.execute(
            select(RepNotice.side, RepNotice.kind, RepNotice.text)
            .where(RepNotice.world_id == ended.world_id, RepNotice.thread_id == ended.thread_id)
            .order_by(RepNotice.side, RepNotice.seq)
        )
        return [(side, kind, text) for side, kind, text in rows.all()]


async def _last_seq(sm: Sessionmaker, world_id: UUID) -> int:
    async with sm() as session:
        found = await session.scalar(select(func.max(Event.seq)).where(Event.world_id == world_id))
        return found or 0


# --- How runs end and begin ------------------------------------------------------------


async def test_seeded_endings_have_a_reason_and_no_runs_and_a_new_thread_has_run_1(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    async with sm() as session:
        ended = list(
            await session.scalars(
                repo.in_world(Thread, world_id).where(Thread.state.in_(threads.TERMINAL_STATES))
            )
        )
        assert ended
        assert all(t.end_reason is not None and t.ended_at is not None for t in ended)
        # Seeded history has no runs, so nothing can reopen it.
        assert not (await session.scalars(repo.in_world(ThreadRun, world_id))).all()
    outreach = await approved_outreach(sm, world_id, NORTHLAKE)
    thread_id = await open_thread(sm, outreach, NORTHLAKE)
    async with sm() as session:
        (run,) = await threads.runs(session, world_id, thread_id)
    assert (run.run, run.start_seq, run.max_turns, run.steer_id) == (1, 0, threads.MAX_TURNS, None)


async def test_begin_opens_the_next_run_and_tells_each_side_its_own_way(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    ended = await _ended(sm, await seeded_world(sm))
    async with unit_of_work(sm, ended.world_id) as uow:
        # A `closed` notice Northlake's last turn never delivered, and one that stays true.
        await context.queue_notice(
            uow,
            thread_id=ended.thread_id,
            side="responder",
            kind="closed",
            text=context.closed_notice(HENDERSON_NAME),
        )
        await context.queue_notice(
            uow,
            thread_id=ended.thread_id,
            side="responder",
            kind="settings",
            text="Notice from Harmony: your sharing description changed.",
        )
        await uow.commit()
    steer_id = await _reopen_steer(sm, ended)
    before = await _last_seq(sm, ended.world_id)

    start_seq = await _begin(sm, ended, steer_id)

    assert start_seq == 2
    async with sm() as session:
        first, second = await threads.runs(session, ended.world_id, ended.thread_id)
        thread = await repo.get_required(session, Thread, ended.world_id, ended.thread_id)
        rep = await session.get(RepSession, (ended.world_id, ended.thread_id, "requester"))
        steer = await repo.get_required(session, Steer, ended.world_id, steer_id)
        request = await repo.get_required(session, NetworkRequest, ended.world_id, ended.request_id)
        events = list(
            await session.scalars(
                select(Event).where(Event.world_id == ended.world_id, Event.seq > before)
            )
        )
    # Run 1 keeps its summaries; Northlake's side, which never took a turn, has none.
    assert (first.requester_summary, first.responder_summary) == (SUMMARY, None)
    assert first.end_reason == "closed"
    assert rep is not None
    assert rep.close_summary is None
    assert (
        second.reopened_by_member_id,
        second.reopened_by_org_id,
        second.reopened_side,
        second.steer_id,
        second.start_seq,
        second.max_turns,
    ) == (ALEX, HENDERSON, "requester", steer_id, 2, 19)
    assert (
        thread.state,
        thread.run,
        thread.side_to_move,
        thread.end_reason,
        thread.closed_by,
        thread.ended_at,
        thread.run_finished_at,
    ) == ("active", 2, "requester", None, None, None, None)
    assert await _notices(sm, ended) == [
        ("requester", "reopened", context.reopened_notice(NORTHLAKE_NAME, by_us=True)),
        (
            "responder",
            "settings",
            "Notice from Harmony: your sharing description changed.",
        ),
        ("responder", "reopened", context.reopened_notice(HENDERSON_NAME, by_us=False)),
    ]
    assert steer.status == "done"
    assert (request.status, request.round) == ("active", 2)
    # The reopen's text goes to Henderson alone; the thread to both parties.
    steer_scopes = {e.org_id for e in events if e.entity_type == "steer"}
    assert steer_scopes == {HENDERSON}
    assert any(e.entity_type == "thread" for e in events)

    # A retried activity writes nothing more.
    assert await _begin(sm, ended, steer_id) == 2
    assert len(await _notices(sm, ended)) == 3
    async with sm() as session:
        request = await repo.get_required(session, NetworkRequest, ended.world_id, ended.request_id)
    assert request.round == 2


async def test_finish_run_stores_the_ending_then_lets_reopen_on(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    ended = await _ended(sm, await seeded_world(sm), finished=False)
    async with sm() as session:
        thread = await repo.get_required(session, Thread, ended.world_id, ended.thread_id)
    assert thread.run_finished_at is None

    async with unit_of_work(sm, ended.world_id) as uow:
        await reopens.finish_run(uow, ended.thread_id, 1, CARRY.model_dump(mode="json"))
        await uow.commit()

    async with sm() as session:
        thread = await repo.get_required(session, Thread, ended.world_id, ended.thread_id)
        (run,) = await threads.runs(session, ended.world_id, ended.thread_id)
    assert thread.run_finished_at is not None
    assert (run.state, run.end_reason, run.closed_by, run.ended_at) == (
        "closed",
        "closed",
        "requester",
        thread.ended_at,
    )
    assert ThreadCarry.model_validate(run.carry) == CARRY
    assert run.finished_at == thread.run_finished_at


async def test_a_request_closes_once_per_round(db_sessionmaker: Sessionmaker) -> None:
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    outreach = await approved_outreach(sm, world_id, NORTHLAKE, CALDER)
    northlake = await open_thread(sm, outreach, NORTHLAKE)
    calder = await open_thread(sm, outreach, CALDER)

    async def end_round(*, from_workflow: bool) -> tuple[bool, int, bool]:
        async with unit_of_work(sm, world_id) as uow:
            ended = await requests.end_round(uow, outreach.request_id, from_workflow=from_workflow)
            request = await repo.get_required(
                uow.session, NetworkRequest, world_id, outreach.request_id
            )
            done = request.workflow_done_at is not None
            await uow.commit()
        return ended.closed, ended.round, done

    async def end(thread_id: UUID) -> None:
        async with unit_of_work(sm, world_id) as uow:
            await threads.set_state(uow, thread_id, "expired", end_reason="quiet")
            await uow.commit()

    await end(northlake)
    # A reopened run ending before the request workflow has seen every first run.
    assert await end_round(from_workflow=False) == (False, 1, False)
    # The workflow's own call, with Calder's thread still live.
    assert await end_round(from_workflow=True) == (False, 1, True)
    await end(calder)
    assert await end_round(from_workflow=False) == (True, 1, True)
    assert await end_round(from_workflow=True) == (True, 1, True)

    async with unit_of_work(sm, world_id) as uow:
        assert (await requests.reopen(uow, outreach.request_id)).round == 2
        # A second thread reopening the same round leaves it there.
        reopened = await requests.reopen(uow, outreach.request_id)
        assert (reopened.status, reopened.round) == ("active", 2)
        await uow.commit()


# --- POST /threads/{id}/reopen -----------------------------------------------------------


@pytest.fixture
def temporal_app(
    app_settings: Settings, db_engine: AsyncEngine, temporal_env: WorkflowEnvironment
) -> FastAPI:
    return create_app(
        app_settings.model_copy(update={"admin_secret": SecretStr(ADMIN_SECRET)}),
        engine=db_engine,
        temporal=temporal_env.client,
    )


async def _world(v: Visitor) -> UUID:
    await v.start()
    return UUID((await v.session())["world_id"])


def _reopen(member: Json, body: str = REOPEN_TEXT) -> Json:
    """A Reopen from a fresh tab: its own client key."""
    return {
        "client_key": str(uuid4()),
        "as_member_id": member["member"]["id"],
        "body": body,
    }


async def _started_with(env: WorkflowEnvironment, handle: WorkflowHandle[Any, Any]) -> ThreadInput:
    """The input the thread's latest run was started with."""
    history = await handle.fetch_history()
    started = history.events[0].workflow_execution_started_event_attributes
    (arg,) = await env.client.data_converter.decode(list(started.input.payloads), [ThreadInput])
    assert isinstance(arg, ThreadInput)
    return arg


async def test_a_reopen_stays_its_sides_own_until_the_run_opens_it(
    temporal_app: FastAPI, temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    sm = db_sessionmaker
    async with visitor(temporal_app) as v:
        ended = await _ended(sm, await _world(v))
        path = f"/threads/{ended.thread_id}/reopen"
        helen = await v.act_as("helen")
        alex = await v.act_as("alex")
        body = _reopen(alex)

        # A switched tab writes nothing and starts nothing.
        switched = {**body, "as_member_id": helen["member"]["id"]}
        problem = await v.json("POST", path, json=switched, expect=409)
        assert problem["code"] == "member_changed"
        async with sm() as session:
            assert not (await session.scalars(repo.in_world(Steer, ended.world_id))).all()

        started = await v.json("POST", path, json=body, expect=202)
        assert await v.json("POST", path, json=body, expect=202) == started
        problem = await v.json("POST", path, json={**body, "body": "Other text."}, expect=422)
        assert problem["code"] == "invalid"
        steer_id = UUID(started["steer_id"])
        handle = temporal_env.client.get_workflow_handle(
            thread_workflow_id(ended.world_id, ended.thread_id)
        )
        assert await (await handle.describe()).memo_value(MEMO_STEER) == str(steer_id)
        arg = await _started_with(temporal_env, handle)
        assert (arg.run, arg.carry, arg.max_turns) == (2, CARRY, 3 + threads.MAX_TURNS)
        assert (arg.guidance, arg.steer_seq) == ({}, {"requester": 1, "responder": 0})
        assert arg.reopen == ReopenInput(
            steer_id=steer_id,
            side="requester",
            org_id=HENDERSON,
            member_id=ALEX,
            body=REOPEN_TEXT,
            seq=1,
            from_state="closed",
        )
        assert arg.deadline_at is not None
        assert arg.deadline_at > datetime.now(UTC) + timedelta(hours=19)
        # The same member again, from another tab: the same attempt, or theirs to wait on.
        assert await v.json("POST", path, json=_reopen(alex), expect=202) == started
        problem = await v.json("POST", path, json=_reopen(alex, "Something else."), expect=409)
        assert problem["code"] == "reopen_in_progress"
        assert problem["reopened_by"]["member"]["slug"] == "alex"

        # Northlake sees the thread as it was, and not who is reopening it.
        await v.act_as("helen")
        detail = await v.json("GET", f"/threads/{ended.thread_id}")
        assert (detail["state"], detail["run"], len(detail["runs"])) == ("closed", 1, 1)
        problem = await v.json("POST", path, json=_reopen(helen), expect=409)
        assert (problem["code"], problem.get("reopened_by")) == ("reopen_in_progress", None)

        await _begin(sm, ended, steer_id)

        problem = await v.json("POST", path, json=_reopen(helen), expect=409)
        assert problem["code"] == "thread_open"
        assert problem["reopened_by"]["org"]["id"] == str(HENDERSON)
        assert problem["reopened_by"]["member"] is None
        detail = await v.json("GET", f"/threads/{ended.thread_id}")
        reopened = detail["runs"][1]["reopened"]
        assert (reopened["side"], reopened["by"], reopened["steer_id"]) == (
            "requester",
            None,
            None,
        )
        assert detail["runs"][0]["ended"]["end_reason"] == "closed"
        workspace = await v.json("GET", f"/threads/{ended.thread_id}/workspace")
        assert workspace["earlier_summaries"] == []

        await v.act_as("alex")
        assert await v.json("POST", path, json=_reopen(alex), expect=202) == started
        problem = await v.json("POST", path, json=_reopen(alex, "Something else."), expect=409)
        assert (problem["code"], problem["reopened_by"]["member"]["slug"]) == (
            "thread_open",
            "alex",
        )
        detail = await v.json("GET", f"/threads/{ended.thread_id}")
        reopened = detail["runs"][1]["reopened"]
        assert (reopened["by"]["slug"], reopened["steer_id"]) == ("alex", str(steer_id))
        workspace = await v.json("GET", f"/threads/{ended.thread_id}/workspace")
        assert workspace["earlier_summaries"] == [{"run": 1, "text": SUMMARY}]


async def test_an_abandoned_attempt_is_settled_and_the_next_one_starts(
    temporal_app: FastAPI, temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    sm = db_sessionmaker
    async with visitor(temporal_app) as v:
        ended = await _ended(sm, await _world(v))
        path = f"/threads/{ended.thread_id}/reopen"
        alex = await v.act_as("alex")
        first = await v.json("POST", path, json=_reopen(alex), expect=202)
        handle = temporal_env.client.get_workflow_handle(
            thread_workflow_id(ended.world_id, ended.thread_id)
        )
        await handle.terminate("the worker lost it")

        second = await v.json("POST", path, json=_reopen(alex, "Try Nov 5."), expect=202)

        assert second["steer_id"] != first["steer_id"]
        async with sm() as session:
            abandoned = await repo.get_required(
                session, Steer, ended.world_id, UUID(first["steer_id"])
            )
        assert (abandoned.status, abandoned.failure_reason) == ("failed", "reopen_failed")
        assert (await _started_with(temporal_env, handle)).reopen is not None
        assert await (await handle.describe()).memo_value(MEMO_STEER) == second["steer_id"]


async def test_what_cant_be_reopened(temporal_app: FastAPI, db_sessionmaker: Sessionmaker) -> None:
    sm = db_sessionmaker
    async with visitor(temporal_app) as v:
        world_id = await _world(v)
        ended = await _ended(sm, world_id)
        unfinished = await _ended(sm, world_id, finished=False)
        path = f"/threads/{ended.thread_id}/reopen"
        alex = await v.act_as("alex")

        for body in ("   ", "x" * 4001):
            await v.request("POST", path, json=_reopen(alex, body), expect=422)
        await v.json("POST", f"/threads/{uuid4()}/reopen", json=_reopen(alex), expect=404)
        seeded = sid("thread", "henderson-northlake-test-ct")
        problem = await v.json("POST", f"/threads/{seeded}/reopen", json=_reopen(alex), expect=409)
        assert (problem["code"], problem["block"]) == ("not_reopenable", "history")
        problem = await v.json(
            "POST", f"/threads/{unfinished.thread_id}/reopen", json=_reopen(alex), expect=409
        )
        assert (problem["code"], problem["block"]) == ("not_reopenable", "unfinished")
        async with sm() as session:
            await session.execute(
                update(Thread)
                .where(Thread.world_id == world_id, Thread.id == ended.thread_id)
                .values(run=reopens.MAX_REOPENS + 1)
            )
            await session.commit()
        problem = await v.json("POST", path, json=_reopen(alex), expect=409)
        assert (problem["code"], problem["block"]) == ("not_reopenable", "limit")

        # Ridgeline isn't a party, and can't see the thread at all.
        dana = await v.act_as("dana")
        problem = await v.json("POST", path, json=_reopen(dana), expect=404)
        assert problem["code"] == "not_found"
