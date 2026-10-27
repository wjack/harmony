"""The model activities (`AgentActivities`) run directly against Postgres, on scripted
models built here:

- A refused reservation leaves the activity with its cap in the error's details and
  tells the demo once (`demo.notice {kind: "budget"}`), whichever cap refused it; any
  other failure tells nobody (3.5).
- Internal Claude hears the chat's send mode when it changed since it was last told, in
  either direction, on new turns only; a retried call carries the same notice (3.10).
- A representative's bare text after a code-written "nothing new" stays private, while
  text answering a message goes out (3.13 E3).
- A consult after the turn sent a message is refused: the send is the turn's one effect
  (FEEDBACK_R2_PLAN decision 1).
- A background org's chat streams `stream.chat_delta` like a featured one's (3.1).
- A request's end is noted to internal Claude once, however often a failed turn's
  updates are handed on.
- A silent update look leaves no chat item and nothing in the next turn's history, and
  a look with no news since Claude last wrote is skipped without a model call.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, cast, get_args
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from harmony.agents.keys import chat_key, look_key
from harmony.agents.models import EscalationResolution
from harmony.agents.runtime import MessageHeld, MessageSent
from harmony.agents.transcript import LOOK_NOTE_PREFIX
from harmony.db import repo
from harmony.db.models import (
    Card,
    ChatMessage,
    Consult,
    Conversation,
    Escalation,
    Event,
    Member,
    NetworkRequest,
    Org,
    RepPolicy,
    Thread,
    ThreadMessage,
)
from harmony.db.models.choices import BudgetKind
from harmony.db.uow import unit_of_work
from harmony.domain import conversations, threads
from harmony.events.bus import StreamMessage
from harmony.events.schemas import StreamChatDelta, StreamDelta
from harmony.llm import HeartbeatCallback, ModelRequest, ModelResult, StreamEventCallback
from harmony.llm.budget import FreeSpend
from harmony.llm.errors import BadRequest, BudgetExhausted, ModelCallError
from harmony.llm.pricing import HAIKU_5_5
from harmony.llm.scripted import (
    CassetteEntry,
    CassetteKey,
    CassetteMatch,
    ScriptedBlock,
    ScriptedModel,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from harmony.llm.wire import as_object, as_str, objects
from harmony.storyrun.checks import engine_checks
from harmony.temporal.activities.agents import (
    AgentActivities,
    CardOutcome,
    InternalTurnInput,
    RepTurnInput,
    RepTurnResult,
    network_note,
)
from harmony.temporal.activities.network import ApplyResolutionInput, NetworkActivities
from harmony.temporal.activities.send_mode import (
    NOTICE_PREFIX,
    SENDS_WITHOUT_REVIEW,
    WAITS_FOR_REVIEW,
)
from harmony.temporal.workflows.payloads import (
    BUDGET_EXHAUSTED_ERROR,
    BudgetExhaustedDetails,
    MemberMessage,
    NetworkUpdate,
)
from tests.integration._e4_world import (
    ALEX,
    HELEN,
    HENDERSON,
    NORTHLAKE,
    SCENARIO,
    propose,
    seeded_world,
    start_chat,
)

type Sessionmaker = async_sessionmaker[AsyncSession]

CHAT = chat_key("alex", 1)
THREAD = "agent-activities"


class _Publisher:
    def __init__(self) -> None:
        self.messages: list[StreamMessage] = []

    async def publish(self, message: StreamMessage) -> None:
        self.messages.append(message)

    async def aclose(self) -> None:
        return None


def _entry(
    org: str, role: str, thread: str, turn: int, step: int, *blocks: ScriptedBlock
) -> tuple[CassetteKey, CassetteEntry]:
    match = CassetteMatch.model_validate(
        {"org": org, "role": role, "thread": thread, "turn": turn, "step": step}
    )
    return (SCENARIO, org, match.role, thread, turn, step), CassetteEntry(
        match=match, content=list(blocks)
    )


def _replies(
    org: str, role: str, thread: str, turns: range, text: str
) -> dict[CassetteKey, CassetteEntry]:
    """Each turn answers with `text` at its first two steps (a resumed turn starts at
    step 1)."""
    return dict(
        _entry(org, role, thread, turn, step, TextBlock(type="text", text=text))
        for turn in turns
        for step in (0, 1)
    )


class _Recording(ScriptedModel):
    """Scripted, keeping every request body. `fail` is raised instead of answering;
    the turns in `crash_turns` die on their first attempt, before anything is stored."""

    def __init__(
        self,
        entries: Mapping[CassetteKey, CassetteEntry],
        *,
        fail: BaseException | None = None,
        crash_turns: frozenset[int] = frozenset(),
        strict_contract: bool = True,
    ) -> None:
        super().__init__(entries, strict_contract=strict_contract)
        self.bodies: list[Mapping[str, JsonValue]] = []
        self._fail = fail
        self._crash_turns = crash_turns

    async def call(
        self,
        request: ModelRequest,
        *,
        on_stream_event: StreamEventCallback | None = None,
        heartbeat: HeartbeatCallback | None = None,
    ) -> ModelResult:
        self.bodies.append(request.body)
        if self._fail is not None:
            raise self._fail
        if request.key.turn in self._crash_turns and request.key.attempt == 1:
            raise RuntimeError("injected crash")
        return await super().call(request, on_stream_event=on_stream_event, heartbeat=heartbeat)


def _agents(sm: Sessionmaker, model: ScriptedModel, publisher: _Publisher) -> AgentActivities:
    return AgentActivities(sm, model, FreeSpend(), publisher, model_override=None)


def _env(*, attempt: int = 1) -> ActivityEnvironment:
    """Where an activity runs as the worker's attempt `attempt` would."""
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, attempt=attempt)
    return env


async def _new_thread(sm: Sessionmaker, world_id: UUID, *, opening: bool) -> UUID:
    """A Henderson -> Northlake thread: with Henderson's opening and Northlake to move,
    or with nothing written yet and Henderson to move."""
    thread_id, request_id = uuid4(), uuid4()
    async with unit_of_work(sm, world_id) as uow:
        uow.session.add(
            NetworkRequest(
                world_id=world_id,
                id=request_id,
                requester_org_id=HENDERSON,
                brief="Powder XRD on 12 air-sensitive cathode powders by Nov 6",
                created_by_member_id=ALEX,
            )
        )
        uow.session.add(
            Thread(
                world_id=world_id,
                id=thread_id,
                request_id=request_id,
                requester_org_id=HENDERSON,
                responder_org_id=NORTHLAKE,
                state="active",
                side_to_move="responder" if opening else "requester",
            )
        )
        if not opening:
            await uow.commit()
            return thread_id
        uow.session.add(
            ThreadMessage(
                world_id=world_id,
                id=uuid4(),
                thread_id=thread_id,
                seq=1,
                from_org_id=HENDERSON,
                kind="message",
                body="Can you run powder XRD on 12 air-sensitive samples by Nov 6?",
                provenance="agent",
            )
        )
        await uow.commit()
    return thread_id


async def _thread_turn(
    sm: Sessionmaker, world_id: UUID, *, deliver_after_seq: int = 0
) -> RepTurnInput:
    """Northlake's first turn on a thread Henderson opened."""
    return RepTurnInput(
        world_id=world_id,
        thread_id=await _new_thread(sm, world_id, opening=True),
        side="responder",
        org_id=NORTHLAKE,
        cassette_thread=THREAD,
        turn=1,
        turn_id=uuid4(),
        deliver_after_seq=deliver_after_seq,
    )


def _chat_turn(
    world_id: UUID,
    conversation_id: UUID,
    turn: int,
    *,
    text: str | None = None,
    card: CardOutcome | None = None,
) -> InternalTurnInput:
    messages = (
        [MemberMessage(message_id=uuid4(), member_id=ALEX, text=text, sent_at=datetime.now(UTC))]
        if text is not None
        else []
    )
    return InternalTurnInput(
        world_id=world_id,
        conversation_id=conversation_id,
        org_id=HENDERSON,
        owner_member_id=ALEX,
        cassette_thread=CHAT,
        turn_id=uuid4(),
        turn=turn,
        first_step=1 if card is not None else 0,
        messages=messages,
        card=card,
    )


async def _budget_notices(sm: Sessionmaker, world_id: UUID) -> int:
    async with sm() as session:
        payloads = await session.scalars(
            select(Event.payload).where(Event.world_id == world_id, Event.type == "demo.notice")
        )
        return sum(1 for payload in payloads if payload.get("kind") == "budget")


def _refusal(kind: BudgetKind) -> BudgetExhausted:
    return BudgetExhausted(
        f"the {kind} cap is reached",
        kind=kind,
        budget=f"test:{kind}",
        cap_usd=Decimal(6),
        committed_usd=Decimal(6),
        requested_usd=Decimal("0.5"),
    )


def _notice(body: Mapping[str, JsonValue]) -> str | None:
    """The system notice a request ends with, if any: only a turn's first call has one."""
    last = objects(body.get("messages"))[-1]
    if last.get("role") != "system":
        return None
    return "".join(as_str(block.get("text")) or "" for block in objects(last.get("content")))


async def _alex(sm: Sessionmaker, world_id: UUID) -> str:
    async with sm() as session:
        member = await session.get_one(Member, (world_id, ALEX))
    return member.name


# --- Refused reservations ----------------------------------------------------------------


@pytest.mark.parametrize("kind", get_args(BudgetKind))
async def test_a_refused_reservation_leaves_with_its_cap_and_tells_the_demo_once(
    db_sessionmaker: Sessionmaker, kind: BudgetKind
) -> None:
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    agents = _agents(sm, _Recording({}, fail=_refusal(kind)), _Publisher())

    with pytest.raises(ApplicationError) as refused:
        await _env().run(agents.rep_turn, await _thread_turn(sm, world_id))

    error = refused.value
    assert (error.type, error.non_retryable) == (BUDGET_EXHAUSTED_ERROR, True)
    assert BudgetExhaustedDetails.model_validate(error.details[0]).kind == kind
    assert await _budget_notices(sm, world_id) == 1


async def test_internal_claude_refused_tells_the_demo_too(db_sessionmaker: Sessionmaker) -> None:
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    conversation_id = await start_chat(sm, world_id)
    agents = _agents(sm, _Recording({}, fail=_refusal("paused")), _Publisher())

    with pytest.raises(ApplicationError) as refused:
        await _env().run(agents.internal_turn, _chat_turn(world_id, conversation_id, 1, text="Hi"))

    assert BudgetExhaustedDetails.model_validate(refused.value.details[0]).kind == "paused"
    assert await _budget_notices(sm, world_id) == 1


@pytest.mark.parametrize(
    "failure",
    [ModelCallError("overloaded"), BadRequest("no cassette entry")],
    ids=["overloaded", "bad-request"],
)
async def test_other_failures_tell_the_demo_nothing(
    db_sessionmaker: Sessionmaker, failure: ModelCallError
) -> None:
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    agents = _agents(sm, _Recording({}, fail=failure), _Publisher())

    with pytest.raises(type(failure)):
        await _env().run(agents.rep_turn, await _thread_turn(sm, world_id))

    assert await _budget_notices(sm, world_id) == 0


# --- The send-mode notice (3.10) ---------------------------------------------------------


async def _set_mode(sm: Sessionmaker, world_id: UUID, conversation_id: UUID, sends: bool) -> None:
    async with unit_of_work(sm, world_id) as uow:
        await conversations.set_send_mode(uow, conversation_id, sends)
        await uow.commit()


async def test_internal_claude_hears_a_changed_send_mode_on_its_next_new_turn(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    conversation_id = await start_chat(sm, world_id)
    alex = await _alex(sm, world_id)
    model = _Recording(_replies("henderson", "internal", CHAT, range(1, 8), "Noted."))
    agents = _agents(sm, model, _Publisher())

    async def first_call_notice(turn: InternalTurnInput) -> str | None:
        calls = len(model.bodies)
        await _env().run(agents.internal_turn, turn)
        return _notice(model.bodies[calls])

    # Never told and reviewing first: nothing to say.
    opening = await first_call_notice(_chat_turn(world_id, conversation_id, 1, text="Hi."))
    assert opening is not None
    assert NOTICE_PREFIX not in opening

    await _set_mode(sm, world_id, conversation_id, True)
    told = await first_call_notice(_chat_turn(world_id, conversation_id, 2, text="Go ahead."))
    assert told == SENDS_WITHOUT_REVIEW.format(member=alex)
    # Told once: the next turn hears nothing more.
    assert await first_call_notice(_chat_turn(world_id, conversation_id, 3, text="Ok.")) is None

    # Changed back while a card is out: the resumed turn says nothing...
    await _set_mode(sm, world_id, conversation_id, False)
    async with unit_of_work(sm, world_id) as uow:
        await conversations.append_member_message(
            uow,
            conversation_id,
            message_id=uuid4(),
            member_id=ALEX,
            text="Ask Northlake.",
            turn_id=uuid4(),
        )
        await uow.commit()
    card_id = await propose(sm, world_id, conversation_id, NORTHLAKE)
    async with sm() as session:
        card = await session.get_one(Card, (world_id, card_id))
    resumed = CardOutcome(card_id=card_id, tool_use_id=card.tool_use_id, content="rejected: no")
    # The turn that raised the card, resumed after its one step.
    turn = _chat_turn(world_id, conversation_id, 4, card=resumed).model_copy(
        update={"turn_id": card.turn_id}
    )
    assert await first_call_notice(turn) is None
    # ...and the next new turn does.
    back = await first_call_notice(_chat_turn(world_id, conversation_id, 5, text="Now?"))
    assert back == WAITS_FOR_REVIEW.format(member=alex)


async def test_a_retried_turn_carries_the_same_send_mode_notice(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    conversation_id = await start_chat(sm, world_id, owner=ALEX)
    model = _Recording(
        _replies("henderson", "internal", CHAT, range(1, 2), "Noted."),
        crash_turns=frozenset({1}),
    )
    agents = _agents(sm, model, _Publisher())
    await _set_mode(sm, world_id, conversation_id, True)
    turn = _chat_turn(world_id, conversation_id, 1, text="Send it to Northlake.")

    with pytest.raises(RuntimeError, match="injected crash"):
        await _env(attempt=1).run(agents.internal_turn, turn)
    await _env(attempt=2).run(agents.internal_turn, turn)

    first, retried = model.bodies
    assert _notice(first) == _notice(retried)
    notice = _notice(retried)
    assert notice is not None
    assert SENDS_WITHOUT_REVIEW.format(member=await _alex(sm, world_id)) in notice


# --- A request's end, noted once ----------------------------------------------------------


@pytest.mark.parametrize(
    "first_call_stored", [True, False], ids=["failed-after-its-first-call", "refused-at-once"]
)
async def test_a_closed_note_handed_on_after_a_failed_turn_goes_out_once(
    db_sessionmaker: Sessionmaker, first_call_stored: bool
) -> None:
    """The workflow hands a failed or refused turn's updates to the next turn. One that
    failed after its first call was stored had delivered the note already; one refused
    on its first call stored nothing, so the next turn carries it."""
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    conversation_id = await start_chat(sm, world_id)
    closed = NetworkUpdate(request_id=uuid4(), kind="closed")
    note = network_note(closed)
    check = ToolUseBlock(
        type="tool_use", name="get_request_status", input={"request_id": str(closed.request_id)}
    )
    entries = _replies("henderson", "internal", CHAT, range(1, 4), "Noted.")
    # Turn 2, woken by the network: it checks the request, then its next call fails (no
    # cassette entry), or it is refused at once.
    entries.update(dict([_entry("henderson", "internal", CHAT, 2, 0, check)]))
    del entries[("harmony-demo", "henderson", "internal", CHAT, 2, 1)]
    if not first_call_stored:
        match = CassetteMatch.model_validate(
            {"org": "henderson", "role": "internal", "thread": CHAT, "turn": 2, "step": 0}
        )
        entries[(SCENARIO, "henderson", "internal", CHAT, 2, 0)] = CassetteEntry(
            match=match, content=[], stop_reason="refusal"
        )
    # A refused call's notice isn't stored, so the next request doesn't extend it: the
    # real API takes that (it only misses the cache); the scripted prefix check doesn't.
    model = _Recording(entries, strict_contract=first_call_stored)
    agents = _agents(sm, model, _Publisher())

    await _env().run(agents.internal_turn, _chat_turn(world_id, conversation_id, 1, text="Hi."))
    woken = _chat_turn(world_id, conversation_id, 2).model_copy(
        update={"network_updates": [closed]}
    )
    if first_call_stored:
        with pytest.raises(BadRequest):
            await _env().run(agents.internal_turn, woken)
    else:
        assert (await _env().run(agents.internal_turn, woken)).stop == "refusal"
    handed_on = _chat_turn(world_id, conversation_id, 3, text="How did it go?").model_copy(
        update={"network_updates": [closed]}
    )
    await _env().run(agents.internal_turn, handed_on)

    last = model.bodies[-1]
    carrying = [m for m in objects(last.get("messages")) if note in json.dumps(m.get("content"))]
    assert [m.get("role") for m in carrying] == ["system"]
    async with sm() as session:
        stored = await session.scalars(
            select(ChatMessage.content_text).where(
                ChatMessage.world_id == world_id, ChatMessage.conversation_id == conversation_id
            )
        )
        assert sum(note in text for text in stored) == 1


# --- Bare text after "nothing new" (3.13 E3) ------------------------------------------------


async def test_bare_text_after_nothing_new_stays_private(db_sessionmaker: Sessionmaker) -> None:
    """Only `send_message` reaches the counterparty (section 5 bug 2). After "nothing
    new", plain text ends the turn; answering their message, the model is told it wasn't
    sent, and sends it."""
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    text = "We're still checking the Nov 4 slot."
    send = ToolUseBlock(type="tool_use", name="send_message", input={"text": text})
    entries = _replies("northlake", "representative", THREAD, range(1, 2), text)
    entries.update(
        dict(
            [
                _entry("northlake", "representative", THREAD, 1, 1, send),
                _entry("northlake", "representative", THREAD, 1, 2, ThinkingBlock(type="thinking")),
            ]
        )
    )

    def agents() -> AgentActivities:
        # One model per thread: both threads play the same cassette key.
        return _agents(sm, _Recording(entries), _Publisher())

    # Henderson's opening was delivered already: the turn opens on "nothing new".
    nothing_new = await _thread_turn(sm, world_id, deliver_after_seq=1)
    new = await _thread_turn(sm, world_id)
    quiet = await _env().run(agents().rep_turn, nothing_new)
    answering = await _env().run(agents().rep_turn, new)

    def sent(result: RepTurnResult) -> list[MessageSent]:
        return [e for e in result.effects if isinstance(e, MessageSent)]

    assert sent(quiet) == []
    assert len(sent(answering)) == 1
    async with sm() as session:
        said = await session.execute(
            select(ThreadMessage.thread_id, ThreadMessage.body).where(
                ThreadMessage.world_id == world_id,
                ThreadMessage.thread_id.in_([nothing_new.thread_id, new.thread_id]),
                ThreadMessage.from_org_id == NORTHLAKE,
            )
        )
    assert [tuple(row) for row in said] == [(new.thread_id, text)]


async def test_bare_text_on_an_ended_thread_stays_private(db_sessionmaker: Sessionmaker) -> None:
    """The last turn after the counterparty closed: nothing more reaches them, so text
    that answers their final message isn't streamed as a message that is never sent."""
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    entries = _replies("northlake", "representative", THREAD, range(1, 2), "Thanks, booked.")
    publisher = _Publisher()
    turn = await _thread_turn(sm, world_id)
    async with sm() as session, session.begin():
        await session.execute(
            update(Org).where(Org.world_id == world_id, Org.id == NORTHLAKE).values(featured=True)
        )
        await session.execute(
            update(Thread)
            .where(Thread.world_id == world_id, Thread.id == turn.thread_id)
            .values(state="closed", side_to_move=None)
        )

    result = await _env().run(_agents(sm, _Recording(entries), publisher).rep_turn, turn)

    assert (result.stop, result.effects) == ("end_turn", [])
    assert not [m for m in publisher.messages if isinstance(m.event, StreamDelta)]


async def test_text_after_a_refused_close_is_never_sent_or_streamed(
    db_sessionmaker: Sessionmaker,
) -> None:
    """3.13 E1, E3: Henderson's first turn tries to close before anything was sent, is
    refused, and ends with an acknowledgement. Bare text is a message only as the turn's
    first answer, so the acknowledgement never becomes the opener."""
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    close = ToolUseBlock(type="tool_use", name="close", input={"summary": "Nothing to ask yet."})
    acknowledged = TextBlock(type="text", text="Understood. I'll end my turn without sending.")
    entries = dict(
        [
            _entry("henderson", "representative", THREAD, 1, 0, close),
            _entry("henderson", "representative", THREAD, 1, 1, acknowledged),
        ]
    )
    publisher = _Publisher()
    turn = RepTurnInput(
        world_id=world_id,
        thread_id=await _new_thread(sm, world_id, opening=False),
        side="requester",
        org_id=HENDERSON,
        cassette_thread=THREAD,
        turn=1,
        turn_id=uuid4(),
    )

    result = await _env().run(_agents(sm, _Recording(entries), publisher).rep_turn, turn)

    assert (result.stop, result.effects) == ("end_turn", [])
    assert not [m for m in publisher.messages if isinstance(m.event, StreamDelta)]
    async with sm() as session:
        written = await session.scalar(
            select(func.count())
            .select_from(ThreadMessage)
            .where(ThreadMessage.world_id == world_id, ThreadMessage.thread_id == turn.thread_id)
        )
    assert written == 0


async def test_a_consult_after_a_send_is_refused_and_the_turn_ends_with_the_send(
    db_sessionmaker: Sessionmaker,
) -> None:
    """Decision 1: Northlake replies, then consults in the same turn. The consult is
    refused and writes nothing, so the turn's one effect is the message and Henderson
    moves next; `harmony run` counts the refusal."""
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    send = ToolUseBlock(
        type="tool_use", name="send_message", input={"text": "Yes, we can run it by Nov 6."}
    )
    consult = ToolUseBlock(
        type="tool_use", name="consult", input={"question": "Tell Helen we said yes."}
    )
    entries = dict(
        [
            _entry("northlake", "representative", THREAD, 1, 0, send),
            _entry("northlake", "representative", THREAD, 1, 1, consult),
            _entry("northlake", "representative", THREAD, 1, 2, TextBlock(type="text", text="Ok.")),
        ]
    )
    turn = await _thread_turn(sm, world_id)

    result = await _env().run(_agents(sm, _Recording(entries), _Publisher()).rep_turn, turn)

    assert result.stop == "end_turn"
    assert [type(effect) for effect in result.effects] == [MessageSent]
    async with sm() as session:
        thread = await session.get(Thread, (world_id, turn.thread_id))
        assert thread is not None
        assert thread.side_to_move == "requester"
        bodies = await session.scalars(
            select(ThreadMessage.body)
            .where(ThreadMessage.world_id == world_id, ThreadMessage.thread_id == turn.thread_id)
            .order_by(ThreadMessage.seq)
        )
        assert list(bodies)[1:] == ["Yes, we can run it by Nov 6."]
        asked = await session.scalar(
            select(func.count())
            .select_from(Consult)
            .where(Consult.world_id == world_id, Consult.thread_id == turn.thread_id)
        )
        assert asked == 0
    checks = await engine_checks(sm, world_id, SCENARIO)
    assert (checks.consults_after_send, checks.closes_after_refused_consult) == (1, 0)


ACK: Final = "Understood. I won't send that."


@pytest.mark.parametrize(
    ("reason", "answer", "holds"),
    [
        pytest.param("", [TextBlock(type="text", text=ACK)], False, id="acknowledged"),
        pytest.param("", [], False, id="empty"),
        pytest.param(
            "Nov 10 works for us instead.",
            [TextBlock(type="text", text=ACK)],
            True,
            id="with-a-reason",
        ),
    ],
)
async def test_text_after_a_declined_held_message_answers_nobody(
    db_sessionmaker: Sessionmaker, reason: str, answer: list[ScriptedBlock], holds: bool
) -> None:
    """In Ask, a held message declined without a reason leaves nothing to answer: text
    that acknowledges it stays private, and an empty answer is a quiet turn, rather than
    asking Helen to decline a new held message again and again. A reason is what the
    representative may say: text after it is never sent, and once told so the model
    sends a message, held for review."""
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    async with sm() as session, session.begin():
        await session.execute(
            update(RepPolicy)
            .where(RepPolicy.world_id == world_id, RepPolicy.org_id == NORTHLAKE)
            .values(autonomy="ask", version=RepPolicy.version + 1)
        )
    first = await _thread_turn(sm, world_id)
    reply = ToolUseBlock(type="tool_use", name="send_message", input={"text": "Nov 4 is free."})
    entries = dict(
        [
            _entry("northlake", "representative", THREAD, 1, 0, reply),
            _entry("northlake", "representative", THREAD, 2, 0, *answer),
        ]
    )
    if holds:
        sends = ToolUseBlock(type="tool_use", name="send_message", input={"text": reason})
        entries.update([_entry("northlake", "representative", THREAD, 2, 1, sends)])
    agents = _agents(sm, _Recording(entries), _Publisher())
    opened = await _env().run(agents.rep_turn, first)
    (held,) = opened.effects
    assert isinstance(held, MessageHeld)
    assert opened.owed is not None
    resolution = EscalationResolution(
        escalation_id=held.escalation_id,
        action="decline",
        member_id=HELEN,
        org_id=NORTHLAKE,
        text=reason or None,
    )
    applied = await NetworkActivities(cast(Client, None), sm).apply_resolution(
        ApplyResolutionInput(
            world_id=world_id, thread_id=first.thread_id, resolution=resolution, message_id=uuid4()
        )
    )
    assert applied.owed is not None
    assert not applied.sent
    # What ThreadWorkflow does next: the same side moves, owed the held call's result.
    async with unit_of_work(sm, world_id) as uow:
        await threads.set_state(uow, first.thread_id, "active", side_to_move="responder")
        await uow.commit()
    owed = applied.owed.model_copy(update={"tool_use_id": opened.owed.tool_use_id})
    again = first.model_copy(
        update={"turn": 2, "turn_id": uuid4(), "owed": [owed], "deliver_after_seq": 1}
    )

    result = await _env().run(agents.rep_turn, again)

    assert result.stop == ("performative" if holds else "end_turn")
    assert [type(e) for e in result.effects] == ([MessageHeld] if holds else [])
    async with sm() as session:
        items = await session.scalar(
            select(func.count())
            .select_from(Escalation)
            .where(
                Escalation.world_id == world_id,
                Escalation.thread_id == first.thread_id,
                Escalation.kind == "outbound_review",
            )
        )
        sent = await session.scalar(
            select(func.count())
            .select_from(ThreadMessage)
            .where(
                ThreadMessage.world_id == world_id,
                ThreadMessage.thread_id == first.thread_id,
                ThreadMessage.from_org_id == NORTHLAKE,
            )
        )
    assert (items, sent) == (2 if holds else 1, 0)


# --- An empty answer to an opening that asks for none (3.13 E1b) -----------------------------


@pytest.mark.parametrize(
    "blocks", [[], [ThinkingBlock(type="thinking")]], ids=["empty", "thinking-only"]
)
async def test_an_empty_answer_to_nothing_new_is_a_quiet_turn(
    db_sessionmaker: Sessionmaker, blocks: list[ScriptedBlock]
) -> None:
    """The thread counts it as a silent turn (E3). An empty answer to the counterparty's
    message is asked once more (`EMPTY_NUDGE`), and a second empty answer is a quiet
    turn too, not a model error (section 5 bug 1). Thinking alone says nothing, and
    `harmony run`'s engine checks count it with the empty ones."""
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    entries = dict(
        _entry("northlake", "representative", THREAD, 1, step, *blocks) for step in (0, 1)
    )

    quiet = await _env().run(
        _agents(sm, _Recording(entries), _Publisher()).rep_turn,
        await _thread_turn(sm, world_id, deliver_after_seq=1),
    )
    # The nudged call sends the notices after the nudge, not after the input, so it
    # can't extend the first call byte for byte: the real API only misses the cache.
    nudged = await _env().run(
        _agents(sm, _Recording(entries, strict_contract=False), _Publisher()).rep_turn,
        await _thread_turn(sm, world_id),
    )

    assert (quiet.stop, quiet.effects, quiet.owed) == ("end_turn", [], None)
    assert (nudged.stop, nudged.next_step, nudged.effects) == ("end_turn", 2, [])
    checks = await engine_checks(sm, world_id, SCENARIO)
    assert (checks.empty_turn_nudges, checks.empty_end_turns_handled) == (1, 2)


@pytest.mark.parametrize(
    ("status", "result"),
    [
        ("approved", "approved; request {request} sent to Northlake Characterization Core"),
        ("rejected", "rejected: Ask Ridgeline instead."),
    ],
)
async def test_an_empty_answer_to_a_sent_card_ends_the_resumed_turn(
    db_sessionmaker: Sessionmaker, status: str, result: str
) -> None:
    """Resumed on its own card once the request went out, the turn has nothing it must
    answer; a rejection's typed feedback still needs a reply, so an empty answer to it
    is asked once more, and a second empty one ends the turn."""
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    conversation_id = await start_chat(sm, world_id)
    async with unit_of_work(sm, world_id) as uow:
        await conversations.append_member_message(
            uow,
            conversation_id,
            message_id=uuid4(),
            member_id=ALEX,
            text="Ask Northlake.",
            turn_id=uuid4(),
        )
        await uow.commit()
    card_id = await propose(sm, world_id, conversation_id, NORTHLAKE)
    async with sm() as session, session.begin():
        card = await session.get_one(Card, (world_id, card_id))
        card.status = "approved" if status == "approved" else "rejected"
    resumed = CardOutcome(
        card_id=card_id, tool_use_id=card.tool_use_id, content=result.format(request=uuid4())
    )
    turn = _chat_turn(world_id, conversation_id, 1, card=resumed).model_copy(
        update={"turn_id": card.turn_id}
    )
    entries = dict(_entry("henderson", "internal", CHAT, 1, step) for step in (1, 2))
    agents = _agents(sm, _Recording(entries), _Publisher())

    ended = await _env().run(agents.internal_turn, turn)

    nudged = status == "rejected"
    assert (ended.stop, ended.next_step, ended.effects) == ("end_turn", 2 + nudged, [])
    # `harmony run`'s engine checks split it the same way, though it came at step 1.
    checks = await engine_checks(sm, world_id, SCENARIO)
    assert (checks.empty_turn_nudges, checks.empty_end_turns_handled) == (nudged, 1)


# --- Streaming in every org (3.1) -----------------------------------------------------------


async def test_a_background_orgs_chat_streams_its_reply(db_sessionmaker: Sessionmaker) -> None:
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    async with sm() as session, session.begin():
        await session.execute(
            update(Org).where(Org.world_id == world_id, Org.id == HENDERSON).values(featured=False)
        )
    conversation_id = await start_chat(sm, world_id)
    reply = "Northlake runs air-free XRD; I'll draft a request."
    model = _Recording(_replies("henderson", "internal", CHAT, range(1, 2), reply))
    publisher = _Publisher()

    await _env().run(
        _agents(sm, model, publisher).internal_turn,
        _chat_turn(world_id, conversation_id, 1, text="Find XRD time."),
    )

    (body,) = model.bodies
    assert body.get("model") == HAIKU_5_5, "a background org's chat runs on Haiku"
    assert "display" not in (as_object(body.get("thinking")) or {})
    deltas = [m.event for m in publisher.messages if isinstance(m.event, StreamChatDelta)]
    assert deltas, "no stream.chat_delta for a background org"
    assert "".join(d.text for d in deltas) == reply


# --- Update looks ---------------------------------------------------------------------


async def _news(sm: Sessionmaker, world_id: UUID, conversation_id: UUID, body: str) -> None:
    """A request from the chat whose Northlake conversation has a reply with `body`."""
    thread_id = await _new_thread(sm, world_id, opening=True)
    async with unit_of_work(sm, world_id) as uow:
        thread = await repo.get_required(uow.session, Thread, world_id, thread_id)
        request = await repo.get_required(uow.session, NetworkRequest, world_id, thread.request_id)
        request.conversation_id = conversation_id
        uow.session.add(
            ThreadMessage(
                world_id=world_id,
                id=uuid4(),
                thread_id=thread_id,
                seq=2,
                from_org_id=NORTHLAKE,
                kind="message",
                body=body,
                provenance="agent",
            )
        )
        await uow.commit()


async def _chat_items(sm: Sessionmaker, world_id: UUID, conversation_id: UUID) -> list[object]:
    async with sm() as session:
        chat = await repo.get_required(session, Conversation, world_id, conversation_id)
        return list(await conversations.chat_items(session, chat))


async def test_a_silent_look_leaves_no_chat_item_and_nothing_in_the_next_turns_history(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    conversation_id = await start_chat(sm, world_id)
    entries = _replies("henderson", "internal", CHAT, range(1, 3), "Noted.")
    # The look checks the request, then ends without writing (the unscripted default).
    entries.update(
        [
            _entry(
                "henderson",
                "internal",
                look_key(CHAT),
                1,
                0,
                ToolUseBlock(type="tool_use", name="get_request_status", input={}),
            )
        ]
    )
    model = _Recording(entries)
    agents = _agents(sm, model, _Publisher())
    await _env().run(agents.internal_turn, _chat_turn(world_id, conversation_id, 1, text="Hi."))
    reply = "We can do Nov 4, air-free."
    await _news(sm, world_id, conversation_id, reply)
    before = await _chat_items(sm, world_id, conversation_id)
    look = _chat_turn(world_id, conversation_id, 1).model_copy(update={"look": True})

    result = await _env().run(agents.internal_turn, look)

    assert (result.stop, result.skipped) == ("end_turn", False)
    opening, checked = model.bodies[-2:]
    assert LOOK_NOTE_PREFIX in json.dumps(opening.get("messages"))
    assert reply in json.dumps(objects(checked.get("messages"))[-1])
    assert await _chat_items(sm, world_id, conversation_id) == before

    await _env().run(
        agents.internal_turn, _chat_turn(world_id, conversation_id, 2, text="Any news?")
    )
    history = json.dumps(model.bodies[-1].get("messages"))
    assert LOOK_NOTE_PREFIX not in history
    assert "get_request_status" not in history
    # Nothing new since Claude last wrote: the next look ends before any model call.
    calls = len(model.bodies)
    again = _chat_turn(world_id, conversation_id, 1).model_copy(update={"look": True})
    assert (await _env().run(agents.internal_turn, again)).skipped
    assert len(model.bodies) == calls
