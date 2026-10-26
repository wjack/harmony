"""Snapshot cursors: the world seq a REST snapshot is consistent with (§8).

A list or snapshot response carries `X-Harmony-Cursor: <seq>`; the client opens its
event stream with `?after=<seq>` and so receives every event the snapshot may have
missed. The cursor is safe because seqs are allocated as each writer's last statement
under a row lock: when `world_event_seq.seq` reads N, every event up to N is committed.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.db.models import WorldEventSeq


async def current_cursor(session: AsyncSession, world_id: UUID) -> int:
    """The world's latest event seq, read in the caller's transaction (0 before any event).

    Under READ COMMITTED (the default) each statement sees a fresh snapshot, so read
    the cursor **before** the data it accompanies: an event committed in between is
    then both in the data and redelivered after the cursor, which clients ignore by
    version. Read after the data, the cursor could pass an event the data missed. In a
    REPEATABLE READ transaction the order does not matter.

    Raises `LookupError` for a world with no event counter (unknown or deleted).
    """
    seq = (
        await session.execute(select(WorldEventSeq.seq).where(WorldEventSeq.world_id == world_id))
    ).scalar_one_or_none()
    if seq is None:
        raise LookupError(f"world {world_id} has no event counter")
    return seq
