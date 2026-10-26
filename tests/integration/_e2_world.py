"""E2 test helpers: a seeded harmony-demo world with a Henderson → Northlake thread."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.db.models import Conversation, Event, NetworkRequest
from harmony.db.models.choices import Side
from harmony.db.uow import unit_of_work
from harmony.domain import threads
from harmony.seeds.ids import SeedKind, seed_id
from harmony.seeds.loader import load_world

type Sessionmaker = async_sessionmaker[AsyncSession]

SCENARIO: Final = "harmony-demo"


def sid(kind: SeedKind, key: str) -> UUID:
    return seed_id(SCENARIO, kind, key)


HENDERSON: Final = sid("org", "henderson")
NORTHLAKE: Final = sid("org", "northlake")
RIDGELINE: Final = sid("org", "ridgeline")
VANTAGE: Final = sid("org", "vantage")
ALEX: Final = sid("member", "alex")
MAYA: Final = sid("member", "maya")
HELEN: Final = sid("member", "helen")
RAJ: Final = sid("member", "raj")
DANA: Final = sid("member", "dana")
JORDAN: Final = sid("member", "jordan")
PRICE_LIST: Final = sid("knowledge_item", "northlake/price-list")
CALENDAR: Final = sid("knowledge_item", "northlake/public-calendar")
RESERVE: Final = sid("knowledge_item", "northlake/reserve-xrd-block")
CLIENT: Final = sid("knowledge_item", "northlake/client-voltaic")


@dataclass(frozen=True)
class Opened:
    world_id: UUID
    thread_id: UUID
    request_id: UUID
    conversation_id: UUID


async def seeded_world(sessionmaker: Sessionmaker) -> UUID:
    return await load_world(sessionmaker, SCENARIO, build_sha="test")


async def open_thread(
    sessionmaker: Sessionmaker,
    world_id: UUID,
    *,
    responder: UUID = NORTHLAKE,
    requester: UUID = HENDERSON,
    sender: UUID = ALEX,
) -> Opened:
    """`sender`'s chat (Alex's by default), a request from it, and a thread to
    `responder`."""
    async with unit_of_work(sessionmaker, world_id) as uow:
        conversation = Conversation(
            world_id=world_id, id=uuid4(), org_id=requester, owner_member_id=sender
        )
        uow.session.add(conversation)
        await uow.session.flush()
        request = NetworkRequest(
            world_id=world_id,
            id=uuid4(),
            requester_org_id=requester,
            conversation_id=conversation.id,
            created_by_member_id=sender,
            brief="Powder XRD on 14 air-sensitive powders by Nov 6, up to $2,000.",
            recipients=[],
        )
        uow.session.add(request)
        await uow.session.flush()
        thread = await threads.open(
            uow, thread_id=uuid4(), request=request, responder_org_id=responder, deadline_at=None
        )
        await uow.commit()
    return Opened(world_id, thread.id, request.id, conversation.id)


async def to_move(sessionmaker: Sessionmaker, opened: Opened, side: Side) -> None:
    """The thread `active` with `side` to move, as the thread workflow leaves it."""
    async with unit_of_work(sessionmaker, opened.world_id) as uow:
        await threads.set_state(uow, opened.thread_id, "active", side_to_move=side)
        await uow.commit()


async def event_count(sessionmaker: Sessionmaker, world_id: UUID) -> int:
    async with sessionmaker() as session:
        count = await session.scalar(
            select(func.count()).select_from(Event).where(Event.world_id == world_id)
        )
        return int(count or 0)
