"""The UnitOfWork's commit contract (`harmony.db.uow`) and the world-scoped repository."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from pydantic import TypeAdapter
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from harmony.db import repo
from harmony.db.models import Event, Member, Org, VisitorSession, World, WorldWorkflow
from harmony.db.uow import unit_of_work
from harmony.errors import WorldRetired
from harmony.events.bus import EVENTS_CHANNEL
from harmony.events.cursor import current_cursor
from harmony.events.schemas import DemoNotice, DurableEvent, EntitySnapshot, EventScope, HelloTick
from harmony.events.schemas import WorldRetired as WorldRetiredEvent
from tests.conftest import ClonedDatabase
from tests.integration._worlds import create_world

type Sessionmaker = async_sessionmaker[AsyncSession]

_events = TypeAdapter[DurableEvent](DurableEvent)


@pytest.fixture
async def world_id(db_sessionmaker: Sessionmaker) -> UUID:
    return await create_world(db_sessionmaker)


def _tick(n: int = 1) -> HelloTick:
    return HelloTick(workflow_id="w", n=n, of=9)


def _workflow(world_id: UUID, workflow_id: str = "w-1") -> WorldWorkflow:
    return WorldWorkflow(world_id=world_id, workflow_id=workflow_id, workflow_type="t")


def _org(world_id: UUID, org_id: UUID, name: str = "Northlake") -> Org:
    return Org(world_id=world_id, id=org_id, slug="northlake", name=name, kind="lab")


def _member(world_id: UUID, member_id: UUID, org_id: UUID) -> Member:
    return Member(
        world_id=world_id, id=member_id, org_id=org_id, slug="helen", name="Helen", role="admin"
    )


async def _commit(sm: Sessionmaker, world_id: UUID, *rows: object, events: int = 1) -> list[int]:
    """One unit adding `rows` and `events` ticks, committed."""
    async with unit_of_work(sm, world_id) as uow:
        uow.session.add_all(rows)
        for n in range(1, events + 1):
            uow.add_event(_tick(n), EventScope.world())
        return list(await uow.commit())


async def _events_of(sm: Sessionmaker, world_id: UUID) -> list[Event]:
    async with sm() as session:
        rows = await session.scalars(
            select(Event).where(Event.world_id == world_id).order_by(Event.seq)
        )
        return list(rows)


async def _cursor(sm: Sessionmaker, world_id: UUID) -> int:
    async with sm() as session:
        return await current_cursor(session, world_id)


async def _retire(sm: Sessionmaker, world_id: UUID) -> None:
    async with unit_of_work(sm, world_id, require_live=False) as uow:
        world = await uow.session.get_one(World, world_id)
        world.status = "retired"
        uow.add_event(WorldRetiredEvent(), EventScope.world())
        await uow.commit()


# --- Creating a world -----------------------------------------------------------------


async def test_creating_a_world_emits_world_created_as_seq_1(
    db_sessionmaker: Sessionmaker, world_id: UUID
) -> None:
    [created] = await _events_of(db_sessionmaker, world_id)
    assert (created.seq, created.type, created.scope_kind) == (1, "world.created", "world")
    assert created.payload == {
        "type": "world.created",
        "scenario": "test",
        "mode": "live",
        "build_sha": "test",
    }
    assert await _cursor(db_sessionmaker, world_id) == 1
    async with db_sessionmaker() as session:
        assert await repo.world_status(session, world_id) == "live"


# --- The commit's last statements -----------------------------------------------------


async def test_commit_ends_with_seq_update_insert_and_notify(
    db_engine: AsyncEngine,
    db_sessionmaker: Sessionmaker,
    database: ClonedDatabase,
    world_id: UUID,
) -> None:
    statements: list[str] = []
    notified: asyncio.Queue[str] = asyncio.Queue()

    def record(*args: Any) -> None:
        statements.append(" ".join(str(args[2]).split()))

    def on_notify(_conn: object, _pid: int, _channel: str, payload: object) -> None:
        notified.put_nowait(str(payload))

    listener = await asyncpg.connect(database.dsn)
    await listener.add_listener(EVENTS_CHANNEL, on_notify)
    event.listen(db_engine.sync_engine, "before_cursor_execute", record)
    try:
        seqs = await _commit(db_sessionmaker, world_id, _workflow(world_id), events=2)
        payload = await asyncio.wait_for(notified.get(), timeout=5)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", record)
        await listener.close()

    assert seqs == [2, 3]
    assert payload == f"{world_id}:3"
    assert statements[-3].startswith("UPDATE world_event_seq SET seq=")
    assert statements[-2].startswith("INSERT INTO events")
    assert statements[-1].startswith("SELECT pg_notify(")
    # The flush, the liveness re-check and the deferred FK checks all come first.
    before = statements[:-3]
    assert any(s.startswith("INSERT INTO world_workflows") for s in before)
    assert any(s.startswith("SELECT worlds.status") and "FOR SHARE" in s for s in before)
    assert "SET CONSTRAINTS ALL IMMEDIATE" in before


async def test_a_unit_without_events_allocates_no_seq(
    db_sessionmaker: Sessionmaker, world_id: UUID
) -> None:
    assert await _commit(db_sessionmaker, world_id, _workflow(world_id), events=0) == []
    assert await _cursor(db_sessionmaker, world_id) == 1
    async with db_sessionmaker() as session:
        assert await session.get(WorldWorkflow, (world_id, "w-1")) is not None


# --- Rollback -------------------------------------------------------------------------


async def test_leaving_without_commit_or_with_an_error_discards_everything(
    db_sessionmaker: Sessionmaker, world_id: UUID
) -> None:
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        uow.session.add(_workflow(world_id, "w-1"))
        uow.add_event(_tick(), EventScope.world())

    async def fail_midway() -> None:
        async with unit_of_work(db_sessionmaker, world_id) as uow:
            uow.session.add(_workflow(world_id, "w-2"))
            uow.add_event(_tick(), EventScope.world())
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await fail_midway()

    assert await _cursor(db_sessionmaker, world_id) == 1
    assert len(await _events_of(db_sessionmaker, world_id)) == 1
    async with db_sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(WorldWorkflow)) == 0


async def test_commit_is_once_only(db_sessionmaker: Sessionmaker, world_id: UUID) -> None:
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        await uow.commit()
        with pytest.raises(RuntimeError, match="already"):
            await uow.commit()
        with pytest.raises(RuntimeError, match="already"):
            uow.add_event(_tick(), EventScope.world())


# --- Deferred foreign keys ------------------------------------------------------------


async def test_rows_can_be_added_children_first_in_one_unit(
    db_sessionmaker: Sessionmaker, world_id: UUID
) -> None:
    org_id, member_id = uuid4(), uuid4()
    # The ORM flushes `members` before `orgs` (class-name order); the FK check waits.
    await _commit(
        db_sessionmaker, world_id, _member(world_id, member_id, org_id), _org(world_id, org_id)
    )
    async with db_sessionmaker() as session:
        member = await repo.get_required(session, Member, world_id, member_id)
    assert member.org_id == org_id


async def test_a_dangling_reference_fails_the_commit_before_any_seq_is_allocated(
    db_sessionmaker: Sessionmaker, world_id: UUID
) -> None:
    orphan = _member(world_id, uuid4(), org_id=uuid4())
    with pytest.raises(IntegrityError, match="fk_members_world_id_org_id_orgs"):
        await _commit(db_sessionmaker, world_id, orphan)
    assert await _cursor(db_sessionmaker, world_id) == 1


# --- Liveness -------------------------------------------------------------------------


async def test_a_retired_world_refuses_commits_and_keeps_nothing(
    db_sessionmaker: Sessionmaker, world_id: UUID
) -> None:
    await _retire(db_sessionmaker, world_id)
    [_, retired] = await _events_of(db_sessionmaker, world_id)
    assert (retired.seq, retired.type) == (2, "world.retired")

    with pytest.raises(WorldRetired):
        await _commit(db_sessionmaker, world_id, _workflow(world_id, "late"))
    assert await _cursor(db_sessionmaker, world_id) == 2
    async with db_sessionmaker() as session:
        assert await session.get(WorldWorkflow, (world_id, "late")) is None


async def test_an_unknown_world_refuses_commits(db_sessionmaker: Sessionmaker) -> None:
    with pytest.raises(WorldRetired):
        await _commit(db_sessionmaker, uuid4(), events=0)


async def test_retirement_waits_for_a_commit_already_past_its_liveness_check(
    db_sessionmaker: Sessionmaker, world_id: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked = asyncio.Event()
    release = asyncio.Event()
    held: set[AsyncSession] = set()
    real_commit = AsyncSession.commit

    async def commit(self: AsyncSession) -> None:
        # Runs after the unit's statements (FOR SHARE included), before COMMIT.
        if self in held:
            checked.set()
            await release.wait()
        await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", commit)

    async def writer() -> list[int]:
        async with unit_of_work(db_sessionmaker, world_id) as uow:
            held.add(uow.session)
            uow.add_event(_tick(), EventScope.world())
            return list(await uow.commit())

    writing = asyncio.create_task(writer())
    await asyncio.wait_for(checked.wait(), timeout=10)
    retiring = asyncio.create_task(_retire(db_sessionmaker, world_id))
    await asyncio.sleep(0.3)
    assert not retiring.done()  # its status UPDATE waits on the writer's FOR SHARE
    release.set()
    assert await asyncio.wait_for(writing, timeout=10) == [2]
    await asyncio.wait_for(retiring, timeout=10)
    events = await _events_of(db_sessionmaker, world_id)
    assert [(e.seq, e.type) for e in events] == [
        (1, "world.created"),
        (2, "hello.tick"),
        (3, "world.retired"),
    ]


# --- Event rows -----------------------------------------------------------------------


async def test_scope_columns_mirror_the_event_scope(
    db_sessionmaker: Sessionmaker, world_id: UUID
) -> None:
    org_id, thread_id, member_id, session_id = uuid4(), uuid4(), uuid4(), uuid4()
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        uow.add_event(_tick(1), EventScope.thread(org_id, thread_id))
        uow.add_event(_tick(2), EventScope.member(member_id, org_id))
        uow.add_event(_tick(3), EventScope.session(session_id))
        uow.add_event(DemoNotice(kind="escalation.created", org_slug="nl"), EventScope.demo())
        await uow.commit()
    rows = (await _events_of(db_sessionmaker, world_id))[1:]
    assert [(r.scope_kind, r.org_id, r.thread_id, r.member_id, r.session_id) for r in rows] == [
        ("thread", org_id, thread_id, None, None),
        ("member", org_id, None, member_id, None),
        ("session", None, None, None, session_id),
        ("demo", None, None, None, None),
    ]
    assert all(r.entity_type is None for r in rows)
    assert isinstance(_events.validate_python(rows[3].payload), DemoNotice)


async def test_demo_scope_takes_only_demo_notices(
    db_sessionmaker: Sessionmaker, world_id: UUID
) -> None:
    snapshot = EntitySnapshot(entity_type="org", entity_id=uuid4(), version=1, data={})
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        with pytest.raises(ValueError, match="demo"):
            uow.add_event(_tick(), EventScope.demo())
        with pytest.raises(ValueError, match="demo"):
            uow.add_event(snapshot, EventScope.demo())
        with pytest.raises(ValueError, match="demo"):
            uow.add_snapshot(_org(world_id, uuid4()), EventScope.demo())


async def test_snapshots_carry_the_version_the_row_was_committed_with(
    db_sessionmaker: Sessionmaker, world_id: UUID
) -> None:
    org_id = uuid4()
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        org = _org(world_id, org_id)
        uow.session.add(org)
        uow.add_snapshot(org, EventScope.org(org_id))
        await uow.commit()

    async with unit_of_work(db_sessionmaker, world_id) as uow:
        org = await repo.get_required(uow.session, Org, world_id, org_id)
        org.name = "Northlake Core"
        # Queued before the change is flushed and built after it, so it says version 2.
        uow.add_snapshot(org, EventScope.org(org_id))
        await uow.commit()

    first, second = (await _events_of(db_sessionmaker, world_id))[1:]
    assert (first.entity_type, first.entity_id, first.entity_version) == ("org", org_id, 1)
    assert (second.entity_type, second.entity_id, second.entity_version) == ("org", org_id, 2)
    snapshot = _events.validate_python(second.payload)
    assert isinstance(snapshot, EntitySnapshot)
    assert snapshot.version == 2
    assert snapshot.data["name"] == "Northlake Core"
    assert snapshot.data["id"] == str(org_id)
    assert isinstance(snapshot.data["created_at"], str)
    assert "world_id" not in snapshot.data


async def test_snapshots_refuse_rows_from_another_world(
    db_sessionmaker: Sessionmaker, world_id: UUID
) -> None:
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        with pytest.raises(ValueError, match="belongs to world"):
            uow.add_snapshot(_org(uuid4(), uuid4()), EventScope.world())


async def test_a_stale_update_raises_instead_of_overwriting(
    db_sessionmaker: Sessionmaker, world_id: UUID
) -> None:
    org_id = uuid4()
    await _commit(db_sessionmaker, world_id, _org(world_id, org_id), events=0)
    async with db_sessionmaker() as first, db_sessionmaker() as second:
        a = await repo.get_required(first, Org, world_id, org_id)
        b = await repo.get_required(second, Org, world_id, org_id)
        a.name = "A"
        await first.commit()
        b.name = "B"
        with pytest.raises(StaleDataError):
            await second.commit()


# --- Cursor ---------------------------------------------------------------------------


async def test_cursor_of_an_unknown_world_is_an_error(db_sessionmaker: Sessionmaker) -> None:
    async with db_sessionmaker() as session:
        with pytest.raises(LookupError):
            await current_cursor(session, uuid4())


# --- Repository -----------------------------------------------------------------------


async def test_seed_ids_repeat_across_worlds_without_crossing(
    db_sessionmaker: Sessionmaker, world_id: UUID
) -> None:
    other_world = await create_world(db_sessionmaker)
    org_id = uuid4()
    await _commit(db_sessionmaker, world_id, _org(world_id, org_id, "first"), events=0)
    await _commit(db_sessionmaker, other_world, _org(other_world, org_id, "second"), events=0)
    async with db_sessionmaker() as session:
        first = await repo.get_required(session, Org, world_id, org_id)
        second = await repo.get_required(session, Org, other_world, org_id)
        assert (first.name, second.name) == ("first", "second")
        assert [o.name for o in await repo.list_in_world(session, Org, other_world)] == ["second"]
        with pytest.raises(repo.NotFoundInWorld):
            await repo.get_required(session, Org, uuid4(), org_id)


async def test_a_session_cannot_act_as_a_member_of_another_world(
    db_sessionmaker: Sessionmaker, world_id: UUID
) -> None:
    other_world = await create_world(db_sessionmaker)
    org_id, member_id = uuid4(), uuid4()
    await _commit(
        db_sessionmaker,
        other_world,
        _org(other_world, org_id),
        _member(other_world, member_id, org_id),
        events=0,
    )
    visitor = VisitorSession(id=uuid4(), desk_world_id=world_id, desk_member_id=member_id)

    async def save() -> None:
        async with db_sessionmaker() as session, session.begin():
            session.add(visitor)

    with pytest.raises(IntegrityError, match="fk_sessions_desk_world_id_desk_member_id_members"):
        await save()


async def test_deleting_a_retired_worlds_rows_keeps_the_tombstone(
    db_sessionmaker: Sessionmaker, world_id: UUID
) -> None:
    org_id, member_id, session_id = uuid4(), uuid4(), uuid4()
    visitor = VisitorSession(id=session_id, desk_world_id=world_id, desk_member_id=member_id)
    await _commit(
        db_sessionmaker,
        world_id,
        _org(world_id, org_id),
        _member(world_id, member_id, org_id),
        visitor,
    )

    async with db_sessionmaker() as session, session.begin():
        with pytest.raises(ValueError, match="live"):
            await repo.delete_world_rows(session, world_id)

    await _retire(db_sessionmaker, world_id)
    async with db_sessionmaker() as session:
        async with session.begin():
            await repo.delete_world_rows(session, world_id)
        assert await repo.world_status(session, world_id) == "retired"
        for table in ("orgs", "members", "events", "world_event_seq"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
        reloaded = await session.get_one(VisitorSession, session_id, populate_existing=True)
        assert (reloaded.desk_world_id, reloaded.desk_member_id) == (None, None)
