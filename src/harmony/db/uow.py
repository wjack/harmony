"""The UnitOfWork: one transaction in one world, plus the events it emits (outbox).

Contract (ARCHITECTURE.md §8):

- A unit is bound to exactly one `world_id` for its whole life. Allocating sequence
  numbers for two worlds in one transaction could deadlock and would break
  commit order = seq order, so cross-world writes are separate units.
- Callers make their state changes through `uow.session` and queue events with
  `add_event(event, scope)` or `add_snapshot(row, scope)`, in the order clients should
  see them. Nothing is written to `events` until `commit()`.
- `commit()` runs, inside the one transaction:
  1. a flush of the session's pending ORM changes (this bumps `Versioned` rows);
  2. unless `require_live=False`, a re-check that `worlds.status = 'live'`
     (`SELECT … FOR SHARE`, so a concurrent retirement's status update waits for this
     commit), raising `harmony.errors.WorldRetired` (and rolling back) otherwise;
  3. if any events were queued: `SET CONSTRAINTS ALL IMMEDIATE`, which runs the
     deferred foreign-key checks now (see `models.base.DEFERRED`), and the snapshot
     events are built from their rows as flushed;
  4. then, **as the last statements of the transaction**, and only if events were
     queued:
       a. `UPDATE world_event_seq SET seq = seq + :n WHERE world_id = :w RETURNING seq`
          (the row lock serializes writers per world, so commit order equals seq
          order);
       b. the `INSERT` into `events` for the n events, numbered `seq - n + 1 .. seq`
          in queue order;
       c. `SELECT pg_notify('events', '<world_id>:<last seq>')`.
     No statement after (a) takes another lock: the insert's only foreign key is to
     the `world_event_seq` row (a) already holds, and it is checked immediately.
  5. `COMMIT`. Returns the allocated seqs (empty when no events were queued).
- Leaving the `async with` block without `commit()`, or with an exception, rolls back
  and discards the queued events. `commit()` may be called at most once per unit; a
  failed commit rolls back and finishes the unit.
- `world_event_seq` rows are created together with their world (`repo.add_world`), in
  the unit that emits `world.created`. The `worlds` row is inserted as `live` in that
  unit and flushed before the re-check, so the re-check passes. `require_live=False`
  is for the retirement path, which emits `world.retired` after changing the status.
- pg_notify is a wake-up hint only; subscribers always read `events` by seq.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from pydantic import JsonValue
from pydantic_core import to_jsonable_python
from sqlalchemy import func, insert, select, text, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.db.models import Event, Snapshotable, World, WorldEventSeq
from harmony.errors import WorldRetired
from harmony.events.bus import EVENTS_CHANNEL
from harmony.events.schemas import DemoNotice, DurableEvent, EntitySnapshot, EventScope

# Rows per INSERT statement; asyncpg allows 32767 bind parameters per statement.
_INSERT_CHUNK = 1000


class UnitOfWork(Protocol):
    """One transaction in one world that collects events and writes them at commit."""

    @property
    def world_id(self) -> UUID:
        """The world every write and event in this unit belongs to."""
        ...

    @property
    def session(self) -> AsyncSession:
        """The unit's session; its transaction is the unit's transaction."""
        ...

    def add_event(self, event: DurableEvent, scope: EventScope) -> None:
        """Queue `event` for this world, to be written at commit.

        An `EntitySnapshot` also fills the row's entity columns (`entity_type`,
        `entity_id`, `entity_version`). A `demo`-scoped event must be a `DemoNotice`;
        anything else raises `ValueError` immediately, so entity data never leaks to
        every viewer.
        """
        ...

    def add_snapshot(self, row: Snapshotable, scope: EventScope) -> None:
        """Queue an `entity.snapshot` of `row`, built at commit after the flush.

        Building it late means the event carries the version the row is committed
        with, even when the row changes again later in the unit. The snapshot's data is
        every column except `world_id`, JSON-encoded (UUIDs, datetimes and decimals as
        strings). For a narrower view, build an `EntitySnapshot` and use `add_event`.
        Raises `ValueError` for a `demo` scope or a row from another world.
        """
        ...

    async def commit(self) -> Sequence[int]:
        """Write the queued events as the transaction's last statements and commit.

        Returns the seqs allocated to the queued events, in queue order.
        Raises `WorldRetired` if the world is not live (unless `require_live=False`).
        """
        ...


@dataclass(frozen=True, slots=True)
class _Queued:
    scope: EventScope
    event: DurableEvent | None = None
    row: Snapshotable | None = None


class SqlUnitOfWork:
    """The Postgres `UnitOfWork`. Build it with `unit_of_work(...)`."""

    def __init__(self, session: AsyncSession, world_id: UUID, *, require_live: bool) -> None:
        self._session = session
        self._world_id = world_id
        self._require_live = require_live
        self._queued: list[_Queued] = []
        self._finished = False

    @property
    def world_id(self) -> UUID:
        return self._world_id

    @property
    def session(self) -> AsyncSession:
        return self._session

    def add_event(self, event: DurableEvent, scope: EventScope) -> None:
        self._ensure_open()
        if scope.kind == "demo" and not isinstance(event, DemoNotice):
            raise ValueError(f"demo scope carries only demo.notice events, not {event.type}")
        self._queued.append(_Queued(scope=scope, event=event))

    def add_snapshot(self, row: Snapshotable, scope: EventScope) -> None:
        self._ensure_open()
        if scope.kind == "demo":
            raise ValueError("demo scope carries only demo.notice events, not snapshots")
        if row.world_id != self._world_id:
            raise ValueError(
                f"{type(row).__name__} belongs to world {row.world_id}, not {self._world_id}"
            )
        self._queued.append(_Queued(scope=scope, row=row))

    async def commit(self) -> Sequence[int]:
        self._ensure_open()
        self._finished = True
        try:
            seqs = await self._write()
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return seqs

    async def _write(self) -> list[int]:
        session = self._session
        await session.flush()
        if self._require_live:
            await self._ensure_live()
        if not self._queued:
            return []
        await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        events = [(await self._event_for(item), item.scope) for item in self._queued]

        # Last statements of the transaction: nothing below may take another lock.
        last = (
            await session.execute(
                update(WorldEventSeq)
                .where(WorldEventSeq.world_id == self._world_id)
                .values(seq=WorldEventSeq.seq + len(events))
                .returning(WorldEventSeq.seq)
            )
        ).scalar_one_or_none()
        if last is None:
            raise LookupError(f"world {self._world_id} has no world_event_seq row")
        first = last - len(events) + 1
        rows = [
            _event_row(self._world_id, first + offset, event, scope)
            for offset, (event, scope) in enumerate(events)
        ]
        for start in range(0, len(rows), _INSERT_CHUNK):
            await session.execute(insert(Event).values(rows[start : start + _INSERT_CHUNK]))
        await session.execute(select(func.pg_notify(EVENTS_CHANNEL, f"{self._world_id}:{last}")))
        return list(range(first, last + 1))

    async def _ensure_live(self) -> None:
        status = (
            await self._session.execute(
                select(World.status).where(World.id == self._world_id).with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if status != "live":
            raise WorldRetired(self._world_id)

    async def _event_for(self, item: _Queued) -> DurableEvent:
        if item.event is not None:
            return item.event
        assert item.row is not None
        return await _snapshot(self._session, item.row)

    def _ensure_open(self) -> None:
        if self._finished:
            raise RuntimeError("this unit of work has already committed or failed")


async def _snapshot(session: AsyncSession, row: Snapshotable) -> EntitySnapshot:
    state = sa_inspect(row, raiseerr=True)
    if state.deleted or state.was_deleted or state.transient:
        raise ValueError(f"cannot snapshot a {type(row).__name__} that is not persisted")
    columns = [attr.key for attr in state.mapper.column_attrs if attr.key != "world_id"]
    unloaded = state.unloaded.intersection(columns)
    if unloaded:
        await session.refresh(row, attribute_names=sorted(unloaded))
    data: dict[str, JsonValue] = {key: _jsonable(getattr(row, key)) for key in columns}
    return EntitySnapshot(
        entity_type=row.entity_type,
        entity_id=row.snapshot_id(),
        version=row.snapshot_version(),
        data=data,
    )


def _jsonable(value: Any) -> JsonValue:
    return to_jsonable_python(value)


def _event_row(world_id: UUID, seq: int, event: DurableEvent, scope: EventScope) -> dict[str, Any]:
    snapshot = event if isinstance(event, EntitySnapshot) else None
    return {
        "world_id": world_id,
        "seq": seq,
        "type": event.type,
        "scope_kind": scope.kind,
        "org_id": scope.org_id,
        "member_id": scope.member_id,
        "thread_id": scope.thread_id,
        "session_id": scope.session_id,
        "entity_type": snapshot.entity_type if snapshot else None,
        "entity_id": snapshot.entity_id if snapshot else None,
        "entity_version": snapshot.version if snapshot else None,
        "payload": event.model_dump(mode="json"),
    }


@asynccontextmanager
async def unit_of_work(
    sessionmaker: async_sessionmaker[AsyncSession],
    world_id: UUID,
    *,
    require_live: bool = True,
) -> AsyncGenerator[UnitOfWork]:
    """Open a unit of work bound to `world_id`.

    Usage::

        async with unit_of_work(sm, world_id) as uow:
            thread = await repo.get_required(uow.session, Thread, world_id, thread_id)
            thread.state = "awaiting_principal"
            uow.add_snapshot(thread, EventScope.thread(org_id, thread_id))
            await uow.commit()
    """
    async with sessionmaker() as session:
        yield SqlUnitOfWork(session, world_id, require_live=require_live)
