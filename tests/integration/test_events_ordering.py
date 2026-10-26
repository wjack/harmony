"""The outbox's ordering guarantees (ARCHITECTURE.md §8; P0 T5 acceptance a and b).

Seqs are allocated as each writer's last statement under the world's counter-row
lock, so commit order equals seq order: a reader that resumes from any cursor never
sees seq N+1 before seq N is visible, and so never skips an event.
"""

from __future__ import annotations

import asyncio
import random
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from harmony.db.models import Event, WorldWorkflow
from harmony.db.uow import unit_of_work
from harmony.events.cursor import current_cursor
from harmony.events.schemas import EventScope, HelloTick
from tests.integration._worlds import create_world

WRITERS = 20
EVENTS_PER_WRITER = 50
TOTAL = WRITERS * EVENTS_PER_WRITER
# create_world emits world.created as seq 1, so the writers' events are 2..TOTAL + 1.
FIRST = 2
LAST = TOTAL + 1


@pytest.fixture
async def world_id(db_sessionmaker: async_sessionmaker[AsyncSession]) -> UUID:
    return await create_world(db_sessionmaker)


def _tick(writer: int, n: int) -> HelloTick:
    return HelloTick(workflow_id=f"writer-{writer}", n=n, of=EVENTS_PER_WRITER)


async def _write(
    sessionmaker: async_sessionmaker[AsyncSession],
    world_id: UUID,
    writer: int,
    rng: random.Random,
) -> list[list[int]]:
    """Commit this writer's events in units of 1-5, each with a state change too."""
    commits: list[list[int]] = []
    n = 0
    while n < EVENTS_PER_WRITER:
        size = min(rng.randint(1, 5), EVENTS_PER_WRITER - n)
        async with unit_of_work(sessionmaker, world_id) as uow:
            uow.session.add(
                WorldWorkflow(world_id=world_id, workflow_id=str(uuid4()), workflow_type="t")
            )
            for _ in range(size):
                n += 1
                uow.add_event(_tick(writer, n), EventScope.world())
            await asyncio.sleep(rng.random() / 500)
            commits.append(list(await uow.commit()))
    return commits


type Delivered = list[tuple[int, dict[str, Any]]]


async def _read_after(
    engine: AsyncEngine, world_id: UUID, after: int, limit: int
) -> list[tuple[int, dict[str, Any]]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(Event.seq, Event.payload)
            .where(Event.world_id == world_id, Event.seq > after)
            .order_by(Event.seq)
            .limit(limit)
        )
        return [(row.seq, row.payload) for row in rows]


async def _subscribe(
    engine: AsyncEngine, world_id: UUID, writers_done: asyncio.Event, rng: random.Random
) -> Delivered:
    """What an SSE subscriber sees: it reconnects (a fresh connection) for every poll and
    resumes past its cursor, asserting each batch continues the sequence with no gap.

    Now and then it also resumes from a random older cursor (a stale Last-Event-ID)
    and checks the replay is exactly what it already saw.
    """
    delivered: Delivered = [(1, {})]  # seq 1 (world.created) predates the stream
    while True:
        finished = writers_done.is_set()
        cursor = delivered[-1][0]
        if rng.random() < 0.3:
            rewind = rng.randint(1, cursor)
            replay = await _read_after(engine, world_id, rewind, cursor - rewind)
            assert replay == delivered[rewind:cursor]
        batch = await _read_after(engine, world_id, cursor, rng.randint(1, 60))
        for seq, payload in batch:
            assert seq == delivered[-1][0] + 1, f"gap after seq {delivered[-1][0]}: got {seq}"
            delivered.append((seq, payload))
        if finished and not batch:
            return delivered
        await asyncio.sleep(0)


async def test_concurrent_writers_deliver_every_seq_once_in_order(
    db_engine: AsyncEngine,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    world_id: UUID,
) -> None:
    rng = random.Random(20261026)
    writers_done = asyncio.Event()

    reader = asyncio.create_task(_subscribe(db_engine, world_id, writers_done, random.Random(7)))
    results = await asyncio.gather(
        *(_write(db_sessionmaker, world_id, w, random.Random(rng.random())) for w in range(WRITERS))
    )
    writers_done.set()
    delivered = await asyncio.wait_for(reader, timeout=30)

    # Each commit got consecutive seqs, and together they cover the range exactly once.
    allocated = [seq for commits in results for unit in commits for seq in unit]
    for commits in results:
        for unit in commits:
            assert unit == list(range(unit[0], unit[0] + len(unit)))
    assert sorted(allocated) == list(range(FIRST, LAST + 1))

    # The reader saw every seq once, in order, with no gaps at any point.
    assert [seq for seq, _ in delivered] == list(range(1, LAST + 1))

    # Each writer's own events appear in the order it wrote them.
    seqs_by_writer: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for seq, payload in delivered[1:]:
        seqs_by_writer[payload["workflow_id"]].append((payload["n"], seq))
    assert len(seqs_by_writer) == WRITERS
    for pairs in seqs_by_writer.values():
        assert [n for n, _ in pairs] == list(range(1, EVENTS_PER_WRITER + 1))
        assert [seq for _, seq in pairs] == sorted(seq for _, seq in pairs)

    async with db_sessionmaker() as session:
        assert await current_cursor(session, world_id) == LAST


async def _poll_until(predicate: Callable[[], Awaitable[bool]], *, attempts: int = 500) -> None:
    for _ in range(attempts):
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never became true")


async def test_a_writer_that_has_its_seq_blocks_the_next_so_commit_order_is_seq_order(
    db_engine: AsyncEngine,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    world_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allocated = asyncio.Event()
    release = asyncio.Event()
    committed: list[str] = []
    held: set[AsyncSession] = set()
    real_commit = AsyncSession.commit

    async def commit(self: AsyncSession) -> None:
        # Runs after the unit's seq UPDATE, INSERT and NOTIFY, before COMMIT.
        if self in held:
            allocated.set()
            await release.wait()
        await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", commit)

    async def slow() -> list[int]:
        async with unit_of_work(db_sessionmaker, world_id) as uow:
            held.add(uow.session)
            uow.add_event(_tick(1, 1), EventScope.world())
            seqs = list(await uow.commit())
            committed.append("slow")
            return seqs

    async def fast() -> list[int]:
        async with unit_of_work(db_sessionmaker, world_id) as uow:
            uow.add_event(_tick(2, 1), EventScope.world())
            seqs = list(await uow.commit())
            committed.append("fast")
            return seqs

    slow_task = asyncio.create_task(slow())
    await asyncio.wait_for(allocated.wait(), timeout=10)
    fast_task = asyncio.create_task(fast())

    async def fast_is_waiting_on_the_seq_lock() -> bool:
        async with db_engine.connect() as conn:
            waiting = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                    " AND wait_event_type = 'Lock' AND query LIKE 'UPDATE world_event_seq%'"
                )
            )
        return bool(waiting)

    await _poll_until(fast_is_waiting_on_the_seq_lock)
    assert not fast_task.done()
    # Neither event is visible yet, so no reader can see seq 3 without seq 2.
    async with db_sessionmaker() as session:
        assert await current_cursor(session, world_id) == 1
    assert await _read_after(db_engine, world_id, 1, 10) == []

    release.set()
    slow_seqs, fast_seqs = await asyncio.wait_for(asyncio.gather(slow_task, fast_task), 10)

    assert committed == ["slow", "fast"]
    assert (slow_seqs, fast_seqs) == ([2], [3])
    rows = await _read_after(db_engine, world_id, 1, 10)
    assert [(seq, payload["workflow_id"]) for seq, payload in rows] == [
        (2, "writer-1"),
        (3, "writer-2"),
    ]
