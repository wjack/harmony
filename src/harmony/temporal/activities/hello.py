"""The P0 hello activity: one `hello.tick` event through the outbox, exactly once."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from harmony.db.models import Event
from harmony.db.uow import unit_of_work
from harmony.events.schemas import EventScope, HelloTick
from harmony.temporal.policies import ensure_world_live


class HelloTickInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    world_id: UUID
    workflow_id: str
    n: int = Field(ge=1)
    of: int = Field(ge=1)


class HelloActivities:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    @activity.defn
    async def emit_hello_tick(self, tick: HelloTickInput) -> int:
        """Emit tick `n` of `of` and return its seq.

        A retry after a commit whose completion never reached Temporal finds the
        committed event and returns its seq instead of emitting a duplicate.
        """
        await ensure_world_live(self._sessionmaker, tick.world_id)
        async with unit_of_work(self._sessionmaker, tick.world_id) as uow:
            existing = await uow.session.scalar(
                select(Event.seq).where(
                    Event.world_id == tick.world_id,
                    Event.type == "hello.tick",
                    Event.payload.contains({"workflow_id": tick.workflow_id, "n": tick.n}),
                )
            )
            if existing is not None:
                return existing
            uow.add_event(
                HelloTick(workflow_id=tick.workflow_id, n=tick.n, of=tick.of),
                EventScope.world(),
            )
            (seq,) = await uow.commit()
        return seq
