"""`agents.transcript.SqlTranscriptStore`: raw wire JSON, append-only, ordered by seq,
and idempotent per (turn, position) so a retried activity never duplicates or
rewrites what an earlier attempt stored (§2.2); and `take_back_in`, a refused turn's one
way out, for a Retry."""

from __future__ import annotations

from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.agents.transcript import (
    ConversationTranscript,
    RepSideTranscript,
    SqlTranscriptStore,
    TranscriptConflict,
    WireMessage,
    append_in,
    take_back_in,
)
from harmony.db.models import ChatMessage, Conversation, Event
from harmony.db.uow import unit_of_work
from harmony.seeds.loader import load_world
from tests.integration._visitors import HISTORY, demo_id

type Sessionmaker = async_sessionmaker[AsyncSession]

ALEX, HENDERSON = demo_id("member", "alex"), demo_id("org", "henderson")
# Key order, non-ASCII and an empty thinking block with its signature survive as sent.
RESPONSE = WireMessage(
    role="assistant",
    content_text='[{"type":"thinking","thinking":"","signature":"sig"},'
    '{"type":"text","text":"Nov 4, 09:00–13:00 works."}]',
)


def user(text: str) -> WireMessage:
    return WireMessage.of("user", [{"type": "text", "text": text}])


@pytest.fixture
async def world(db_sessionmaker: Sessionmaker) -> UUID:
    return await load_world(db_sessionmaker, "harmony-demo", build_sha="test")


async def conversation(sm: Sessionmaker, world_id: UUID) -> ConversationTranscript:
    conversation_id = uuid4()
    async with unit_of_work(sm, world_id) as uow:
        uow.session.add(
            Conversation(
                world_id=world_id, id=conversation_id, org_id=HENDERSON, owner_member_id=ALEX
            )
        )
        await uow.commit()
    return ConversationTranscript(world_id=world_id, conversation_id=conversation_id)


async def test_messages_round_trip_byte_for_byte(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    store = SqlTranscriptStore(db_sessionmaker)
    ref = RepSideTranscript(world_id=world, thread_id=HISTORY, side="responder")
    turn = uuid4()

    seqs = await store.append(ref, [user("Can you run XRD?"), RESPONSE], turn_id=turn, position=0)

    assert seqs == [1, 2]
    loaded = await store.load(ref)
    assert [m.content_text for m in loaded] == [
        user("Can you run XRD?").content_text,
        RESPONSE.content_text,
    ]
    assert {m.turn_id for m in loaded} == {turn}


async def test_appends_are_idempotent_per_position(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    store = SqlTranscriptStore(db_sessionmaker)
    ref = RepSideTranscript(world_id=world, thread_id=HISTORY, side="requester")
    turn = uuid4()
    await store.append(ref, [user("one")], turn_id=turn, position=0)

    # A retry that stored the first message already appends only what's new.
    assert await store.append(ref, [user("one"), RESPONSE], turn_id=turn, position=0) == [1, 2]
    assert await store.append(ref, [RESPONSE], turn_id=turn, position=1) == [2]
    assert len(await store.load(ref)) == 2

    with pytest.raises(TranscriptConflict, match="different message"):
        await store.append(ref, [user("rewritten")], turn_id=turn, position=0)
    with pytest.raises(TranscriptConflict, match="can't append at 5"):
        await store.append(ref, [user("gap")], turn_id=turn, position=5)
    # Without a position, messages go after the turn's own.
    assert await store.append(ref, [user("three")], turn_id=turn) == [3]


async def test_a_turn_that_is_no_longer_the_tail_cannot_append(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    store = SqlTranscriptStore(db_sessionmaker)
    ref = RepSideTranscript(world_id=world, thread_id=HISTORY, side="responder")
    first, second = uuid4(), uuid4()
    await store.append(ref, [user("first")], turn_id=first, position=0)
    await store.append(ref, [user("second")], turn_id=second, position=0)

    with pytest.raises(TranscriptConflict, match="tail"):
        await store.append(ref, [RESPONSE], turn_id=first, position=1)


async def test_chat_messages_carry_their_author_and_bump_the_conversation(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    store = SqlTranscriptStore(db_sessionmaker)
    ref = await conversation(db_sessionmaker, world)
    turn = uuid4()

    await store.append(
        ref, [user("Find an XRD lab"), RESPONSE], turn_id=turn, author_member_id=ALEX, position=0
    )

    async with db_sessionmaker() as session:
        rows = (
            await session.scalars(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == ref.conversation_id)
                .order_by(ChatMessage.seq)
            )
        ).all()
        assert [(r.role, r.author_member_id) for r in rows] == [("user", ALEX), ("assistant", None)]
        assert [r.id for r in rows] == [uuid5(turn, "transcript/0"), uuid5(turn, "transcript/1")]
        snapshot = await session.scalar(
            select(Event).where(
                Event.world_id == world,
                Event.entity_type == "conversation",
                Event.entity_id == ref.conversation_id,
            )
        )
        assert snapshot is not None
        assert (snapshot.scope_kind, snapshot.member_id) == ("member", ALEX)


async def test_append_in_writes_with_the_callers_unit_of_work(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    ref = await conversation(db_sessionmaker, world)
    async with unit_of_work(db_sessionmaker, world) as uow:
        await append_in(uow, ref, [user("typed")], turn_id=uuid4(), author_member_id=ALEX)
        # Nothing is visible before the caller commits.
        assert await SqlTranscriptStore(db_sessionmaker).load(ref) == []
        await uow.commit()
    assert len(await SqlTranscriptStore(db_sessionmaker).load(ref)) == 1

    other = ConversationTranscript(world_id=uuid4(), conversation_id=ref.conversation_id)
    async with unit_of_work(db_sessionmaker, world) as uow:
        with pytest.raises(ValueError, match="appended in"):
            await append_in(uow, other, [user("x")], turn_id=uuid4())


async def test_a_refused_turn_is_taken_back_only_while_nothing_answered_it(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    store = SqlTranscriptStore(db_sessionmaker)
    chat = await conversation(db_sessionmaker, world)
    side = RepSideTranscript(world_id=world, thread_id=HISTORY, side="requester")

    async def take_back(ref: ConversationTranscript | RepSideTranscript, turn_id: UUID) -> bool:
        async with unit_of_work(db_sessionmaker, world) as uow:
            taken = await take_back_in(uow, ref, turn_id)
            await uow.commit()
        return taken

    answered, refused = uuid4(), uuid4()
    await store.append(chat, [user("Find an XRD lab"), RESPONSE], turn_id=answered, position=0)
    await store.append(chat, [user("one"), user("two")], turn_id=refused, position=0)

    # A turn holding a response stays; the refused tail turn's input goes, at once.
    assert not await take_back(chat, answered)
    assert await take_back(chat, refused)
    assert [m.turn_id for m in await store.load(chat)] == [answered, answered]
    assert await take_back(chat, refused)
    # The Retry is a new turn after what's left.
    assert await store.append(chat, [user("one, reworded")], turn_id=uuid4(), position=0) == [3]

    # A representative's input that something came after stays too.
    earlier, later = uuid4(), uuid4()
    await store.append(side, [user("Can you run XRD?")], turn_id=earlier, position=0)
    await store.append(side, [user("Any news?")], turn_id=later, position=0)
    assert not await take_back(side, earlier)
    assert await take_back(side, later)
    assert [m.turn_id for m in await store.load(side)] == [earlier]
