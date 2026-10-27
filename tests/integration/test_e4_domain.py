"""E4's domain services against Postgres: threads, notes, cards, conversations,
requests and the world clock."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.agents import consult as answerer
from harmony.agents.discovery import Shortlist
from harmony.agents.keys import thread_key
from harmony.agents.models import (
    AssistantTextItem,
    CardResolution,
    EscalationResolution,
    InlineQuestionItem,
    OutreachCardItem,
    OutreachEdits,
    Route,
    RouteAssignee,
    ScreenedCounts,
    ToolActivityItem,
    UserTextItem,
)
from harmony.api.routes.threads import ThreadMessageView
from harmony.db import repo
from harmony.db.models import (
    Card,
    Conversation,
    Event,
    InternalNote,
    NetworkRequest,
    RepSession,
    Thread,
    ThreadAccess,
    ThreadMessage,
    World,
)
from harmony.db.models.choices import ThreadMessageKind
from harmony.db.uow import unit_of_work
from harmony.domain import (
    cards,
    clock,
    consults,
    conversations,
    escalations,
    notes,
    records,
    requests,
    routing,
    threads,
)
from harmony.domain.errors import CardNotPending
from tests.integration._e4_world import (
    ALEX,
    CALDER,
    HELEN,
    HENDERSON,
    NORTHLAKE,
    RAJ,
    RIDGELINE,
    approved_outreach,
    consult_draft,
    open_thread,
    propose,
    routed_consult,
    seeded_world,
    sid,
    start_chat,
)

type Sessionmaker = async_sessionmaker[AsyncSession]


async def _thread(sessionmaker: Sessionmaker, world_id: UUID, thread_id: UUID) -> Thread:
    async with sessionmaker() as session:
        return await repo.get_required(session, Thread, world_id, thread_id)


async def _events(sessionmaker: Sessionmaker, world_id: UUID, entity_type: str) -> list[Event]:
    async with sessionmaker() as session:
        rows = await session.scalars(
            select(Event)
            .where(Event.world_id == world_id, Event.entity_type == entity_type)
            .order_by(Event.seq)
        )
        return list(rows)


# --- Threads --------------------------------------------------------------------------


async def test_opening_a_thread_routes_it_to_the_request_creator(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await seeded_world(db_sessionmaker)
    outreach = await approved_outreach(db_sessionmaker, world_id, NORTHLAKE)
    thread_id = await open_thread(db_sessionmaker, outreach, NORTHLAKE)

    thread = await _thread(db_sessionmaker, world_id, thread_id)
    assert (thread.state, thread.side_to_move, thread.turn_count) == ("active", "requester", 0)
    async with db_sessionmaker() as session:
        access = list(
            await session.scalars(
                select(ThreadAccess).where(
                    ThreadAccess.world_id == world_id, ThreadAccess.thread_id == thread_id
                )
            )
        )
    assert [(row.member_id, row.reason) for row in access] == [(ALEX, "routed")]


async def test_messages_take_the_next_seq_and_count_turns_not_messages(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await seeded_world(db_sessionmaker)
    outreach = await approved_outreach(db_sessionmaker, world_id, NORTHLAKE)
    thread_id = await open_thread(db_sessionmaker, outreach, NORTHLAKE)
    henderson_turn, northlake_turn = uuid4(), uuid4()
    # Northlake's representative sends two messages in one turn (§5).
    sent: list[tuple[UUID, UUID | None, ThreadMessageKind, str]] = [
        (HENDERSON, henderson_turn, "message", "Can you run 14 powders?"),
        (NORTHLAKE, northlake_turn, "message", "Yes, Nov 4."),
        (NORTHLAKE, northlake_turn, "message", "Air-free holders are included."),
        (HENDERSON, None, "close", "Thanks, we'll book it."),
    ]
    ids = [uuid4() for _ in sent]
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        for message_id, (org_id, turn_id, kind, body) in zip(ids, sent, strict=True):
            await threads.append_message(
                uow,
                thread_id,
                message_id=message_id,
                from_org_id=org_id,
                kind=kind,
                body=body,
                provenance="agent" if turn_id else "human",
                turn_id=turn_id,
                author_member_id=None if turn_id else ALEX,
            )
        # A retried write returns the row it wrote.
        again = await threads.append_message(
            uow,
            thread_id,
            message_id=ids[0],
            from_org_id=HENDERSON,
            kind="message",
            body="different",
            provenance="agent",
            turn_id=henderson_turn,
        )
        assert again.seq == 1
        assert again.body == "Can you run 14 powders?"
        await uow.commit()

    thread = await _thread(db_sessionmaker, world_id, thread_id)
    assert thread.turn_count == 3
    assert thread.side_to_move == "responder"
    async with db_sessionmaker() as session:
        seqs = await session.scalars(
            select(ThreadMessage.seq)
            .where(ThreadMessage.world_id == world_id, ThreadMessage.thread_id == thread_id)
            .order_by(ThreadMessage.seq)
        )
        assert list(seqs) == [1, 2, 3, 4]
    events = await _events(db_sessionmaker, world_id, threads.THREAD_MESSAGE_ENTITY)
    # Each message reaches both parties' thread scopes, once.
    assert len(events) == 2 * len(sent)
    assert {(e.org_id, e.thread_id) for e in events} == {
        (HENDERSON, thread_id),
        (NORTHLAKE, thread_id),
    }
    # Each stream carries what `GET /threads/{id}` shows: the provenance, never who wrote
    # or approved a message, nor its turn.
    for event in events:
        data = event.payload["data"]
        assert isinstance(data, dict)
        assert set(data) == set(ThreadMessageView.model_fields) | {"thread_id"}
    closing = events[-1].payload["data"]
    assert isinstance(closing, dict)
    assert (closing["provenance"], closing["body"]) == ("human", "Thanks, we'll book it.")


async def test_a_stranger_cannot_write_to_a_thread(db_sessionmaker: Sessionmaker) -> None:
    world_id = await seeded_world(db_sessionmaker)
    outreach = await approved_outreach(db_sessionmaker, world_id, NORTHLAKE)
    thread_id = await open_thread(db_sessionmaker, outreach, NORTHLAKE)
    with pytest.raises(ValueError, match="not a party"):
        async with unit_of_work(db_sessionmaker, world_id) as uow:
            await threads.append_message(
                uow,
                thread_id,
                message_id=uuid4(),
                from_org_id=RIDGELINE,
                kind="message",
                body="Hello?",
                provenance="agent",
            )


async def test_a_terminal_thread_keeps_its_state_and_how_it_ended(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await seeded_world(db_sessionmaker)
    outreach = await approved_outreach(db_sessionmaker, world_id, NORTHLAKE)
    thread_id = await open_thread(db_sessionmaker, outreach, NORTHLAKE)
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        with pytest.raises(ValueError, match="end_reason"):
            await threads.set_state(uow, thread_id, "closed")
        await threads.set_state(uow, thread_id, "awaiting_principal", side_to_move="responder")
        await threads.set_state(
            uow, thread_id, "closed", end_reason="closed", closed_by="responder"
        )
        await threads.set_state(uow, thread_id, "active", side_to_move="requester")
        await threads.set_state(
            uow, thread_id, "expired", end_reason="stopped", stop_reason="model_error"
        )
        await uow.commit()
    thread = await _thread(db_sessionmaker, world_id, thread_id)
    assert (thread.state, thread.side_to_move) == ("closed", None)
    assert (thread.end_reason, thread.closed_by, thread.stop_reason) == (
        "closed",
        "responder",
        None,
    )
    assert thread.ended_at is not None


async def test_cassette_keys_count_only_unseeded_threads_per_pair(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await seeded_world(db_sessionmaker)
    outreach = await approved_outreach(db_sessionmaker, world_id, NORTHLAKE, CALDER)
    first = await open_thread(db_sessionmaker, outreach, NORTHLAKE)
    second = await open_thread(db_sessionmaker, outreach, NORTHLAKE)
    calder = await open_thread(db_sessionmaker, outreach, CALDER)
    seeded = sid("thread", "henderson-northlake-precursor-xrd")
    async with db_sessionmaker() as session:
        keys = [
            await threads.cassette_key(
                session, await repo.get_required(session, Thread, world_id, t)
            )
            for t in (first, second, calder, seeded)
        ]
    assert keys == [
        thread_key("henderson", "northlake", 1),
        thread_key("henderson", "northlake", 2),
        thread_key("henderson", "calder", 1),
        thread_key("", "", seeded_slug="henderson-northlake-precursor-xrd"),
    ]


# --- Notes ----------------------------------------------------------------------------


def test_mentions_are_distinct_slugs_in_order() -> None:
    assert notes.parse_mentions("@raj can you check? cc @helen, @raj. me@example.com") == [
        "raj",
        "helen",
    ]


async def test_a_mention_shares_the_thread_with_a_teammate(db_sessionmaker: Sessionmaker) -> None:
    world_id = await seeded_world(db_sessionmaker)
    outreach = await approved_outreach(db_sessionmaker, world_id, NORTHLAKE)
    thread_id = await open_thread(db_sessionmaker, outreach, NORTHLAKE)
    note_id = uuid4()
    body = "@raj can we hold Nov 4? (@alex is theirs, @nobody isn't anyone)"

    async def add() -> InternalNote:
        async with unit_of_work(db_sessionmaker, world_id) as uow:
            note = await notes.add(
                uow,
                note_id=note_id,
                thread_id=thread_id,
                org_id=NORTHLAKE,
                author_kind="member",
                author_member_id=HELEN,
                body=body,
                mentions=notes.parse_mentions(body),
            )
            await uow.commit()
        return note

    await add()
    note = await add()
    assert note.seq == 1
    # Only Northlake's own members are mentioned.
    assert note.mentions == ["raj"]
    async with db_sessionmaker() as session:
        shared = list(
            await session.scalars(
                select(ThreadAccess).where(
                    ThreadAccess.world_id == world_id,
                    ThreadAccess.thread_id == thread_id,
                    ThreadAccess.reason == "shared",
                )
            )
        )
        listed = await notes.list_for(
            session, world_id=world_id, thread_id=thread_id, org_id=NORTHLAKE
        )
        henderson = await notes.list_for(
            session, world_id=world_id, thread_id=thread_id, org_id=HENDERSON
        )
    assert [(s.member_id, s.shared_by_member_id, s.question) for s in shared] == [
        (RAJ, HELEN, body)
    ]
    assert [n.id for n in listed] == [note_id]
    assert henderson == []
    events = await _events(db_sessionmaker, world_id, notes.INTERNAL_NOTE_ENTITY)
    assert [(e.org_id, e.thread_id) for e in events] == [(NORTHLAKE, thread_id)]


async def test_an_empty_note_is_refused(db_sessionmaker: Sessionmaker) -> None:
    world_id = await seeded_world(db_sessionmaker)
    outreach = await approved_outreach(db_sessionmaker, world_id, NORTHLAKE)
    thread_id = await open_thread(db_sessionmaker, outreach, NORTHLAKE)
    with pytest.raises(ValueError, match="characters"):
        async with unit_of_work(db_sessionmaker, world_id) as uow:
            await notes.add(
                uow,
                note_id=uuid4(),
                thread_id=thread_id,
                org_id=NORTHLAKE,
                author_kind="member",
                author_member_id=HELEN,
                body="   ",
            )


# --- Cards and conversations ----------------------------------------------------------


async def test_a_new_card_supersedes_the_pending_one(db_sessionmaker: Sessionmaker) -> None:
    world_id = await seeded_world(db_sessionmaker)
    conversation_id = await start_chat(db_sessionmaker, world_id)
    first = await propose(db_sessionmaker, world_id, conversation_id, NORTHLAKE)
    second = await propose(db_sessionmaker, world_id, conversation_id, CALDER)
    async with db_sessionmaker() as session:
        pending = await cards.pending(session, world_id=world_id, conversation_id=conversation_id)
        old = await repo.get_required(session, Card, world_id, first)
    assert pending is not None
    assert pending.id == second
    assert old.status != "pending"


async def test_a_card_resolves_once(db_sessionmaker: Sessionmaker) -> None:
    world_id = await seeded_world(db_sessionmaker)
    conversation_id = await start_chat(db_sessionmaker, world_id)
    card_id = await propose(db_sessionmaker, world_id, conversation_id, NORTHLAKE)
    rejected = CardResolution(
        card_id=card_id, decision="reject", member_id=ALEX, feedback="Ask Calder too."
    )
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        card = await cards.resolve(uow, rejected)
        await uow.commit()
    assert card.status == "rejected"
    assert cards.resolution_of(card) == rejected
    with pytest.raises(CardNotPending):
        async with unit_of_work(db_sessionmaker, world_id) as uow:
            await cards.resolve(
                uow, CardResolution(card_id=card_id, decision="approve", member_id=ALEX)
            )


async def test_a_rejected_card_says_whether_a_decline_or_a_message_turned_it_down(
    db_sessionmaker: Sessionmaker,
) -> None:
    """The chat folds a Decline into a "Not sent" record, and a card a message replaced
    into an earlier version, so its item carries which it was."""
    world_id = await seeded_world(db_sessionmaker)
    conversation_id = await start_chat(db_sessionmaker, world_id)
    declined = await propose(db_sessionmaker, world_id, conversation_id, NORTHLAKE)
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        await cards.resolve(
            uow,
            CardResolution(
                card_id=declined, decision="reject", member_id=ALEX, rejected_with="decline"
            ),
        )
        await uow.commit()
    replaced = await propose(db_sessionmaker, world_id, conversation_id, CALDER)
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        await cards.resolve(
            uow,
            CardResolution(
                card_id=replaced,
                decision="reject",
                member_id=ALEX,
                feedback="Ask Calder too.",
                rejected_with="message",
            ),
        )
        await uow.commit()
    pending = await propose(db_sessionmaker, world_id, conversation_id, NORTHLAKE)
    async with db_sessionmaker() as session:
        conversation = await repo.get_required(session, Conversation, world_id, conversation_id)
        items = await conversations.chat_items(session, conversation)
    rejected_with = {
        item.card_id: item.rejected_with for item in items if isinstance(item, OutreachCardItem)
    }
    assert rejected_with == {declined: "decline", replaced: "message", pending: None}


async def test_a_chat_is_titled_from_its_first_message_and_lists_its_items(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await seeded_world(db_sessionmaker)
    conversation_id = await start_chat(db_sessionmaker, world_id, "  Find XRD time\nfor 14 powders")
    turn_id = uuid4()
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        await conversations.append_member_message(
            uow,
            conversation_id,
            message_id=uuid4(),
            member_id=ALEX,
            text="Find XRD time for 14 powders",
            turn_id=turn_id,
        )
        await conversations.append_assistant_content(
            uow,
            conversation_id,
            turn_id=turn_id,
            content_text=json.dumps([{"type": "text", "text": "I'll search the network."}]),
        )
        await uow.commit()
    async with db_sessionmaker() as session:
        conversation = await repo.get_required(session, Conversation, world_id, conversation_id)
        items = await conversations.chat_items(session, conversation)
        recents = await conversations.recents(session, world_id=world_id, owner_member_id=ALEX)
        turns = await conversations.turns_taken(session, conversation)
    # Whitespace collapses onto one line.
    assert conversation.title == "Find XRD time for 14 powders"
    long = conversations.title_from("word " * 40)
    assert long is not None
    assert len(long) <= conversations.TITLE_MAX_CHARS
    assert long.endswith("word…")
    user, assistant = items
    assert isinstance(user, UserTextItem)
    assert (user.text, user.member_id) == ("Find XRD time for 14 powders", ALEX)
    assert isinstance(assistant, AssistantTextItem)
    assert (assistant.markdown, assistant.turn_id) == ("I'll search the network.", turn_id)
    assert [row.id for row in recents] == [conversation_id]
    assert turns == 1


async def test_a_tool_row_runs_in_the_present_and_ends_with_what_it_found(
    db_sessionmaker: Sessionmaker,
) -> None:
    """Decision 5: a row reads "Searching…" until its result is stored, then in the past
    tense with what a search found; the need or query is its second line."""
    world_id = await seeded_world(db_sessionmaker)
    conversation_id = await start_chat(db_sessionmaker, world_id)
    turn_id = uuid4()
    need = "Powder XRD and SEM/EDS on 12 air-sensitive cathode powders by Nov 6."

    def found(strong: int, possible: int) -> str:
        counts = ScreenedCounts(
            total=14, strong=strong, possible=possible, no=14 - strong - possible, unknown=0
        )
        return Shortlist(search_id=uuid4(), screened=counts, entries=[]).model_dump_json()

    def use(tool_id: str, name: str, text: str) -> dict[str, object]:
        key = "need" if name == "search_network" else "query"
        return {"type": "tool_use", "id": tool_id, "name": name, "input": {key: text}}

    calls: list[tuple[str, str, str, str | None]] = [
        ("toolu_both", "search_network", need, found(1, 2)),
        ("toolu_possible", "search_network", need, found(0, 2)),
        ("toolu_none", "search_network", need, found(0, 0)),
        ("toolu_running", "search_network", need, None),
        ("toolu_matches", "search_org_knowledge", "argon holder", '{"results":[{},{}]}'),
        ("toolu_nothing", "search_org_knowledge", "XPS", f"{records.NO_MATCHES} 'XPS'."),
        ("toolu_reading", "search_org_knowledge", "XRD", None),
    ]
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        await conversations.append_assistant_content(
            uow,
            conversation_id,
            turn_id=turn_id,
            content_text=json.dumps([use(tool_id, name, text) for tool_id, name, text, _ in calls]),
        )
        await conversations.append_turn_input(
            uow,
            conversation_id,
            turn_id=turn_id,
            role="user",
            content_text=json.dumps(
                [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": result}
                    for tool_id, _, _, result in calls
                    if result is not None
                ]
            ),
        )
        await uow.commit()
    async with db_sessionmaker() as session:
        conversation = await repo.get_required(session, Conversation, world_id, conversation_id)
        items = await conversations.chat_items(session, conversation)
    rows = [item for item in items if isinstance(item, ToolActivityItem)]
    assert [(row.label, row.outcome, row.detail, row.status) for row in rows] == [
        ("Searched the Harmony network", "1 strong, 2 possible of 14", need, "done"),
        ("Searched the Harmony network", "2 possible fits of 14", need, "done"),
        ("Searched the Harmony network", "No fits among 14", need, "done"),
        ("Searching the Harmony network…", None, need, "running"),
        ("Searched your organization's knowledge", "2 matches", "argon holder", "done"),
        ("Searched your organization's knowledge", "No matches", "XPS", "done"),
        ("Searching your organization's knowledge", None, "XRD", "running"),
    ]


async def ask_alex_side(
    sessionmaker: Sessionmaker, world_id: UUID, thread_id: UUID, question: str, assignee: str
) -> tuple[UUID, UUID]:
    """Henderson's representative asks its own side `question`, routed to `assignee`.
    Returns the escalation and consult ids."""
    consult_id, escalation_id = uuid4(), uuid4()
    async with unit_of_work(sessionmaker, world_id) as uow:
        await consults.open(
            uow,
            consult_id=consult_id,
            thread_id=thread_id,
            side="requester",
            org_id=HENDERSON,
            question=question,
            turn_id=uuid4(),
            tool_use_id=f"toolu_{consult_id.hex[:8]}",
        )
        escalation, _ = await escalations.open(
            uow,
            escalation_id=escalation_id,
            org_id=HENDERSON,
            kind="consult",
            dedupe_key=f"consult:{consult_id}",
            thread_id=thread_id,
            side="requester",
            consult_id=consult_id,
        )
        await routing.apply_route(
            uow, escalation, Route(assignees=[RouteAssignee(member=assignee, why="r")])
        )
        await uow.commit()
    return escalation_id, consult_id


async def test_only_consults_routed_to_the_chats_owner_appear_inline(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await seeded_world(db_sessionmaker)
    outreach = await approved_outreach(db_sessionmaker, world_id, NORTHLAKE)
    thread_id = await open_thread(db_sessionmaker, outreach, NORTHLAKE)

    async def ask(question: str, assignee: str = "alex") -> tuple[UUID, UUID]:
        return await ask_alex_side(db_sessionmaker, world_id, thread_id, question, assignee)

    seal, seal_consult = await ask("Which seal material does the holder need?")
    await ask("Northlake proposes $1,410; go ahead?", "maya")
    budget, budget_consult = await ask("What is the project's budget?")
    slot, _ = await ask("Can the samples arrive Monday?")
    drafted = consult_draft("Kalrez, per the holder's spec sheet.")
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        await escalations.add_draft(uow, seal, drafted, turn_id=None, draft_rev=1)
        # Approved as drafted: the representative is released the quoted tool result.
        await escalations.resolve(
            uow,
            EscalationResolution(
                escalation_id=seal, action="approve", member_id=ALEX, org_id=HENDERSON, draft_rev=1
            ),
        )
        await consults.release(
            uow,
            seal_consult,
            text=answerer.result_text("approve", drafted.answer),
            released_by="member",
            member_id=ALEX,
        )
        await escalations.resolve(
            uow,
            EscalationResolution(
                escalation_id=budget,
                action="decline",
                member_id=ALEX,
                org_id=HENDERSON,
                text="Say we don't share budgets.",
            ),
        )
        await consults.decline(
            uow,
            budget_consult,
            text=answerer.result_text("decline", "Say we don't share budgets."),
            member_id=ALEX,
        )
        await escalations.cancel(uow, slot, reason="thread expired")
        await uow.commit()

    async with db_sessionmaker() as session:
        conversation = await repo.get_required(
            session, Conversation, world_id, outreach.conversation_id
        )
        items = await conversations.chat_items(session, conversation)
    inline = [item for item in items if isinstance(item, InlineQuestionItem)]
    assert {i.from_org.id for i in inline} == {NORTHLAKE}
    # What the member let through, never the representative's tool result; a declined
    # item says so, and one whose thread ended stays, as no longer needed.
    assert [(i.question, i.status, i.action, i.answer) for i in inline] == [
        (
            "Which seal material does the holder need?",
            "resolved",
            "approve",
            "Kalrez, per the holder's spec sheet.",
        ),
        ("What is the project's budget?", "resolved", "decline", "Say we don't share budgets."),
        ("Can the samples arrive Monday?", "cancelled", None, None),
    ]


async def test_an_item_opening_or_settling_reaches_both_parties_and_the_chat(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await seeded_world(db_sessionmaker)
    outreach = await approved_outreach(db_sessionmaker, world_id, NORTHLAKE)
    thread_id = await open_thread(db_sessionmaker, outreach, NORTHLAKE)

    async def mark() -> tuple[int, int]:
        async with db_sessionmaker() as session:
            last = await session.scalar(
                select(func.max(Event.seq)).where(Event.world_id == world_id)
            )
            chat = await repo.get_required(
                session, Conversation, world_id, outreach.conversation_id
            )
        return last or 0, chat.version

    async def since(marked: tuple[int, int]) -> tuple[set[UUID | None], bool]:
        """Which orgs got the thread's snapshot since `marked`, and whether the chat moved."""
        events = await _events(db_sessionmaker, world_id, "thread")
        _, version = await mark()
        orgs = {e.org_id for e in events if e.seq > marked[0] and e.entity_id == thread_id}
        return orgs, version > marked[1]

    # Northlake's representative asks its side (one commit); the answerer then hands the
    # question to a person (another): Henderson's status for Northlake turns to waiting
    # on a person only with the second.
    consult_id = uuid4()
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        await consults.open(
            uow,
            consult_id=consult_id,
            thread_id=thread_id,
            side="responder",
            org_id=NORTHLAKE,
            question="Can you run 14 powders before Nov 6?",
            turn_id=uuid4(),
            tool_use_id="toolu_consult",
        )
        await uow.commit()
    marked = await mark()
    northlake_item = uuid4()
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        await escalations.open(
            uow,
            escalation_id=northlake_item,
            org_id=NORTHLAKE,
            kind="consult",
            dedupe_key=f"consult:{consult_id}",
            thread_id=thread_id,
            side="responder",
            consult_id=consult_id,
        )
        await uow.commit()
    assert await since(marked) == ({HENDERSON, NORTHLAKE}, False)

    marked = await mark()
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        await escalations.resolve(
            uow,
            EscalationResolution(
                escalation_id=northlake_item, action="approve", member_id=HELEN, org_id=NORTHLAKE
            ),
        )
        await uow.commit()
    assert await since(marked) == ({HENDERSON, NORTHLAKE}, False)

    # Henderson's own question also shows in Alex's chat, which refetches as it changes.
    marked = await mark()
    henderson_item, _ = await ask_alex_side(
        db_sessionmaker, world_id, thread_id, "Which seal material?", "alex"
    )
    assert await since(marked) == ({HENDERSON, NORTHLAKE}, True)

    marked = await mark()
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        await escalations.cancel(uow, henderson_item, reason="thread expired")
        await uow.commit()
    assert await since(marked) == ({HENDERSON, NORTHLAKE}, True)


# --- Requests -------------------------------------------------------------------------


async def test_an_approved_card_becomes_a_request_with_the_members_edits(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await seeded_world(db_sessionmaker)
    conversation_id = await start_chat(db_sessionmaker, world_id)
    card_id = await propose(db_sessionmaker, world_id, conversation_id, NORTHLAKE, CALDER)
    request_id = uuid4()
    resolution = CardResolution(
        card_id=card_id,
        decision="approve",
        member_id=ALEX,
        edits=OutreachEdits(recipients=[NORTHLAKE], brief="XRD on 14 powders, by Nov 6."),
    )

    async def approve() -> NetworkRequest:
        async with unit_of_work(db_sessionmaker, world_id) as uow:
            card = await repo.get_required(uow.session, Card, world_id, card_id)
            if card.status == "pending":
                card = await cards.resolve(uow, resolution)
            request = await requests.create_from_card(uow, card, resolution, request_id=request_id)
            await uow.commit()
        return request

    await approve()
    request = await approve()
    assert request.id == request_id
    assert request.brief == "XRD on 14 powders, by Nov 6."
    assert [r.org_id for r in requests.recipients_of(request)] == [NORTHLAKE]
    assert request.created_by_member_id == ALEX
    assert request.conversation_id == conversation_id
    # The org the member left out stays on the status, screened, after the approved one.
    async with db_sessionmaker() as session:
        view = await requests.status(session, request)
    assert [(row.org.id, row.participant, row.thread_id) for row in view.recipients] == [
        (NORTHLAKE, "screened", None),
        (CALDER, "screened", None),
    ]


async def test_request_status_follows_each_recipient(db_sessionmaker: Sessionmaker) -> None:
    world_id = await seeded_world(db_sessionmaker)
    outreach = await approved_outreach(db_sessionmaker, world_id, NORTHLAKE, CALDER)
    thread_id = await open_thread(db_sessionmaker, outreach, NORTHLAKE)
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        await threads.append_message(
            uow,
            thread_id,
            message_id=uuid4(),
            from_org_id=NORTHLAKE,
            kind="message",
            body="Let me check.",
            provenance="agent",
        )
        await uow.commit()
    await routed_consult(db_sessionmaker, world_id, thread_id)

    async with db_sessionmaker() as session:
        request = await repo.get_required(session, NetworkRequest, world_id, outreach.request_id)
        own = await requests.status(session, request)
        lens = await requests.status(session, request, waiting_of="any")
        northlake = await requests.status(session, request, waiting_of=NORTHLAKE)
    by_org = {row.org.id: row for row in own.recipients}
    assert by_org[NORTHLAKE].participant == "needs_human"
    assert by_org[NORTHLAKE].thread_id == thread_id
    # Calder's thread hasn't opened: it was screened.
    assert (by_org[CALDER].participant, by_org[CALDER].thread_id) == ("screened", None)
    # Who holds up the other side is theirs to know.
    assert by_org[NORTHLAKE].waiting_on is None
    for view in (lens, northlake):
        waiting = {row.org.id: row for row in view.recipients}[NORTHLAKE].waiting_on
        assert waiting is not None
        assert waiting.member.id == HELEN


async def test_request_status_shows_the_latest_line_and_only_the_requesters_summary(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await seeded_world(db_sessionmaker)
    outreach = await approved_outreach(db_sessionmaker, world_id, NORTHLAKE)
    thread_id = await open_thread(db_sessionmaker, outreach, NORTHLAKE)

    async def participant() -> str:
        async with db_sessionmaker() as session:
            request = await repo.get_required(
                session, NetworkRequest, world_id, outreach.request_id
            )
            return (await requests.status(session, request)).recipients[0].participant

    assert await participant() == "messaged"
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        await threads.append_message(
            uow,
            thread_id,
            message_id=uuid4(),
            from_org_id=HENDERSON,
            kind="message",
            body="Can you run 14 powders?",
            provenance="agent",
        )
        await uow.commit()
    assert await participant() == "messaged"
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        for org_id, body in ((NORTHLAKE, "Yes: Nov 4.\nDetails follow."), (HENDERSON, "Booked.")):
            await threads.append_message(
                uow,
                thread_id,
                message_id=uuid4(),
                from_org_id=org_id,
                kind="message" if org_id == NORTHLAKE else "close",
                body=body,
                provenance="agent",
            )
        await uow.commit()
    assert await participant() == "replied"
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        await threads.set_state(
            uow, thread_id, "closed", end_reason="closed", closed_by="requester"
        )
        for side, summary in (("requester", "Booked Nov 4."), ("responder", "Sold a slot.")):
            uow.session.add(
                RepSession(
                    world_id=world_id,
                    thread_id=thread_id,
                    side=side,
                    system_text="",
                    tools_text="",
                    model="test",
                    close_summary=summary,
                )
            )
        await uow.commit()

    async with db_sessionmaker() as session:
        request = await repo.get_required(session, NetworkRequest, world_id, outreach.request_id)
        view = await requests.status(session, request)
    (row,) = view.recipients
    assert (row.participant, row.thread_state) == ("closed", "closed")
    # A close isn't a line of the conversation; the last message is, with who sent it.
    assert row.latest_line == "Northlake Characterization Core: Yes: Nov 4."
    assert row.close_summary == "Booked Nov 4."


async def test_a_closed_request_stays_closed(db_sessionmaker: Sessionmaker) -> None:
    world_id = await seeded_world(db_sessionmaker)
    outreach = await approved_outreach(db_sessionmaker, world_id, NORTHLAKE)
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        await requests.set_status(uow, outreach.request_id, "closed")
        request = await requests.set_status(uow, outreach.request_id, "active")
        await uow.commit()
    assert request.status == "closed"


# --- World clock ----------------------------------------------------------------------


async def test_the_world_clock_runs_from_the_anchor_plus_fast_forward(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await seeded_world(db_sessionmaker)
    anchor = clock.scenario_anchor("harmony-demo")
    assert anchor is not None
    async with db_sessionmaker() as session:
        world = await session.get(World, world_id)
        assert world is not None
        created = world.created_at
        assert await clock.today(session, world_id, created + timedelta(hours=1)) == anchor
        assert await clock.today(session, world_id, created + timedelta(days=2)) == anchor + (
            timedelta(days=2)
        )
    async with db_sessionmaker() as session:
        await session.execute(
            update(World).where(World.id == world_id).values(ff_offset_s=3 * 86_400)
        )
        await session.commit()
    async with db_sessionmaker() as session:
        assert await clock.today(session, world_id, created) == anchor + timedelta(days=3)
        world = await session.get(World, world_id)
        assert world is not None
        at = datetime.combine(anchor, datetime.min.time(), tzinfo=UTC) + timedelta(days=5)
        assert clock.world_now(world, anchor, clock.wall_clock(world, anchor, at)) == at
    assert clock.scenario_anchor("no-such-scenario") is None
