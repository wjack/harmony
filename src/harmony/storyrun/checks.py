"""The engine checks `harmony run` reports, read from the world's rows after the run
(LIVE_DEMO_PLAN 3.14, section 5.2). Each says how the engine behaved, not whether the
story's beats came:

- closes: `early_closes` (a close before the counterparty's first message: the guard
  failed), `close_refusals` (the guard fired), `waiting_closes` (a close whose summary
  still waits on someone, or in the same turn as a question the side just asked);
- turns: `empty_turn_failures` and `empty_end_turns_handled` (`end_turn` responses of
  internal and representative turns that say nothing, thinking alone included: on the
  first call of a run, which fail it unless its opening asks for no answer, and on
  later ones, which end it),
  `multi_send_turns` (with the second and later messages), `silent_turn_pairs`;
- messages: `meta_notes` (notes about the conversation sent as messages),
  `cross_mentions` (a requester naming another recipient of its request),
  `opener_leaks` (a scenario's own patterns over the first message of each thread
  whose request went to other recipients too);
- `send_network_request`: `not_run_sends`, `missing_field_errors`, the titles and their
  word counts, and for chats that send without review, `auto_sends` (with any recipient
  that wasn't suggested and screened), `auto_requests_per_turn` and
  `duplicate_auto_recipients` (an auto request to an org an earlier request from its chat
  was still in touch with);
- drafts: those with no `short_question` or one over 20 words, and routes with an
  assignee but no why.

Collection is SQL; the rules are pure functions over the rows.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from uuid import UUID

from pydantic import JsonValue, ValidationError
from sqlalchemy import or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.agents.consult import HELD_DECLINED
from harmony.agents.models import OutreachCardPayload, Route
from harmony.agents.runtime import NOT_RUN, NOT_RUN_YET, echo_content, says_nothing
from harmony.agents.tools.internal import AUTO_SEND_FITS, SEND_NETWORK_REQUEST
from harmony.agents.tools.rep import NOT_REPLIED, NOTHING_SENT
from harmony.db import repo
from harmony.db.models import (
    AgentTurn,
    Card,
    ChatMessage,
    Consult,
    Escalation,
    EscalationDraft,
    Event,
    ModelCall,
    NetworkRequest,
    Org,
    RepSession,
    RepTranscript,
    Screening,
    Thread,
    ThreadMessage,
)
from harmony.domain.requests import recipients_of
from harmony.domain.threads import TERMINAL_STATES
from harmony.llm.wire import JsonObject, as_object, as_str, objects
from harmony.temporal.activities.agents import NOTHING_NEW

type Sessionmaker = async_sessionmaker[AsyncSession]

WAITING: Final = re.compile(
    r"(?i)await|waiting (for|on)|next step|pending (their|a) (reply|response|confirmation)"
    r"|will (review|follow up)"
)
META_NOTE: Final = re.compile(r"(?i)^\s*\[|awaiting your reply|note for |sent by claude for")
# What a thread's first message to a recipient must not say, by scenario and recipient
# (section 5.2): another recipient's part of the request.
OPENER_LEAKS: Final[Mapping[str, Mapping[str, re.Pattern[str]]]] = {
    "harmony-demo": {
        "ridgeline": re.compile(
            r"(?i)\bSEM\b|\bEDS\b|characteri[sz]ation core|Northlake Characteri"
            r"|run (the )?XRD|XRD (and|on) \d+"
        ),
        "northlake": re.compile(r"(?i)Ridgeline|holder|machin"),
    }
}
SHORT_QUESTION_WORDS: Final = 20
# The guard's refusals of an early close, as stored in the representative's transcript.
_CLOSE_REFUSED: Final = (
    NOT_REPLIED.partition("{counterparty} ")[2],
    NOTHING_SENT.partition("{counterparty}")[2],
)
_NOT_RUN: Final = (NOT_RUN.partition(":")[0], NOT_RUN_YET.partition(":")[0])
_INVALID_SEND: Final = f"Invalid input for {SEND_NETWORK_REQUEST}"
_NOTHING_NEW: Final = re.compile(
    re.escape(NOTHING_NEW.partition("{counterparty}")[0])
    + ".+"
    + re.escape(NOTHING_NEW.partition("{counterparty}")[2])
)


@dataclass(frozen=True, slots=True)
class Message:
    thread_id: UUID
    seq: int
    from_org_id: UUID
    kind: str
    body: str
    turn_id: UUID | None


@dataclass(frozen=True, slots=True)
class ThreadRow:
    id: UUID
    request_id: UUID
    requester_org_id: UUID
    responder_org_id: UUID
    title: str | None


@dataclass(frozen=True, slots=True)
class EmptyEndTurn:
    """An `end_turn` response of an internal or representative turn that says nothing
    (`says_nothing`), with the user message its request ended on (its opening, on a
    run's first call) and whether notices followed it."""

    turn_id: UUID
    step_idx: int
    opening: list[JsonObject]
    notices: bool


@dataclass(frozen=True, slots=True)
class SentRequest:
    id: UUID
    conversation_id: UUID
    created_at: datetime
    # Sent without review: its card was `auto`.
    auto: bool
    recipients: list[UUID]


@dataclass
class EngineChecks:
    early_closes: list[str] = field(default_factory=list[str])
    close_refusals: int = 0
    waiting_closes: list[str] = field(default_factory=list[str])
    empty_turn_failures: int = 0
    empty_end_turns_handled: int = 0
    # Per turn that sent several messages: the second and later ones.
    multi_send_turns: list[list[str]] = field(default_factory=list[list[str]])
    silent_turn_pairs: int = 0
    meta_notes: list[str] = field(default_factory=list[str])
    cross_mentions: list[str] = field(default_factory=list[str])
    opener_leaks: list[str] = field(default_factory=list[str])
    openers_checked: int = 0
    not_run_sends: int = 0
    missing_field_errors: int = 0
    titles: list[str] = field(default_factory=list[str])
    drafts: int = 0
    drafts_without_short_question: int = 0
    long_short_questions: list[str] = field(default_factory=list[str])
    routes_without_why: int = 0
    auto_sends: int = 0
    auto_send_breaches: list[str] = field(default_factory=list[str])
    auto_requests_per_turn: int = 0
    duplicate_auto_recipients: list[str] = field(default_factory=list[str])


# --- Collecting -----------------------------------------------------------------------


async def engine_checks(sessionmaker: Sessionmaker, world_id: UUID, scenario: str) -> EngineChecks:
    checks = EngineChecks()
    async with sessionmaker() as session:
        orgs = await repo.list_in_world(session, Org, world_id)
        names = {org.id: org.name for org in orgs}
        slugs = {org.id: org.slug for org in orgs}
        threads = [
            ThreadRow(t.id, t.request_id, t.requester_org_id, t.responder_org_id, t.title)
            for t in await repo.list_in_world(session, Thread, world_id)
        ]
        messages = sorted(
            (
                Message(m.thread_id, m.seq, m.from_org_id, m.kind, m.body, m.turn_id)
                for m in await repo.list_in_world(session, ThreadMessage, world_id)
            ),
            key=lambda m: (m.thread_id, m.seq),
        )
        summaries = {
            (row.thread_id, row.side): row.close_summary
            for row in await repo.list_in_world(session, RepSession, world_id)
        }
        checks.close_refusals = await _close_refusals(session, world_id)
        await _empty_end_turns(session, world_id, checks)
        checks.silent_turn_pairs = await _silent_turn_pairs(session, world_id, messages)
        await _sends(session, world_id, checks)
        requests = list(await repo.list_in_world(session, NetworkRequest, world_id))
        await _drafts(session, world_id, checks)
        await _auto_sends(session, world_id, names, checks)
    _closes(threads, messages, summaries, names, checks)
    _messages(threads, messages, names, slugs, scenario, checks)
    from_chats = {r.id: r for r in requests if r.conversation_id is not None}
    checks.titles = [
        *(_title("request", r.title) for r in from_chats.values()),
        *(
            _title(f"thread to {names.get(t.responder_org_id, '?')}", t.title)
            for t in threads
            if t.request_id in from_chats
        ),
    ]
    return checks


async def _close_refusals(session: AsyncSession, world_id: UUID) -> int:
    rows = await session.scalars(
        select(RepTranscript.content_text).where(
            RepTranscript.world_id == world_id,
            RepTranscript.role == "user",
            or_(*(RepTranscript.content_text.contains(r, autoescape=True) for r in _CLOSE_REFUSED)),
        )
    )
    return sum(_tool_results(text).count(r) for text in rows for r in _CLOSE_REFUSED)


async def _empty_end_turns(session: AsyncSession, world_id: UUID, checks: EngineChecks) -> None:
    # Internal and representative turns store their messages under their turn id; the
    # screener, answerer and sharing check store none and never run the turn loop.
    turns = select(ChatMessage.turn_id).where(ChatMessage.world_id == world_id)
    turns = turns.union(select(RepTranscript.turn_id).where(RepTranscript.world_id == world_id))
    rows = await session.execute(
        select(ModelCall.turn_id, ModelCall.step_idx, ModelCall.response_text).where(
            ModelCall.world_id == world_id, ModelCall.turn_id.in_(turns)
        )
    )
    empty: list[tuple[UUID, int]] = []
    for turn_id, step_idx, response_text in rows:
        response = as_object(json.loads(response_text)) or {}
        # Thinking alone says nothing, as the runtime counts it.
        if response.get("stop_reason") == "end_turn" and says_nothing(echo_content(response)):
            empty.append((turn_id, step_idx))
    if not empty:
        return
    bodies = await session.execute(
        select(ModelCall.turn_id, ModelCall.step_idx, ModelCall.request_text).where(
            ModelCall.world_id == world_id, tuple_(ModelCall.turn_id, ModelCall.step_idx).in_(empty)
        )
    )
    sent_cards = {
        (card.turn_id, card.tool_use_id): card.status == "approved"
        for card in await repo.list_in_world(session, Card, world_id)
    }
    for turn_id, step_idx, request_text in bodies:
        call = _empty_end_turn(turn_id, step_idx, request_text)
        if empty_end_turn_failed(call, sent_cards):
            checks.empty_turn_failures += 1
        else:
            checks.empty_end_turns_handled += 1


def _empty_end_turn(turn_id: UUID, step_idx: int, request_text: str) -> EmptyEndTurn:
    body = as_object(json.loads(request_text)) or {}
    messages = objects(body.get("messages"))
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    return EmptyEndTurn(
        turn_id=turn_id,
        step_idx=step_idx,
        opening=objects(last_user.get("content")) if last_user is not None else [],
        notices=bool(messages) and messages[-1].get("role") == "system",
    )


async def _silent_turn_pairs(
    session: AsyncSession, world_id: UUID, messages: Sequence[Message]
) -> int:
    turns = (
        await session.execute(
            select(AgentTurn.id, AgentTurn.thread_id)
            .where(AgentTurn.world_id == world_id, AgentTurn.role == "representative")
            .order_by(AgentTurn.completed_at, AgentTurn.created_at)
        )
    ).all()
    consulted = await session.scalars(
        select(Consult.turn_id).where(Consult.world_id == world_id, Consult.turn_id.is_not(None))
    )
    held = await session.scalars(
        select(EscalationDraft.turn_id)
        .join(
            Escalation,
            (Escalation.world_id == EscalationDraft.world_id)
            & (Escalation.id == EscalationDraft.escalation_id),
        )
        .where(
            EscalationDraft.world_id == world_id,
            Escalation.kind == "outbound_review",
            EscalationDraft.turn_id.is_not(None),
        )
    )
    acted = {
        turn_id
        for turn_id in (*(m.turn_id for m in messages), *consulted, *held)
        if turn_id is not None
    }
    closing = {m.turn_id for m in messages if m.kind == "close" and m.turn_id is not None}
    responses = await session.execute(
        select(ModelCall.turn_id, ModelCall.response_text).where(
            ModelCall.world_id == world_id,
            ModelCall.turn_id.in_([turn_id for turn_id, _ in turns]),
            ModelCall.response_text.contains('"refusal"'),
        )
    )
    refused = {
        turn_id
        for turn_id, response_text in responses
        if (as_object(json.loads(response_text)) or {}).get("stop_reason") == "refusal"
    }
    return silent_turn_pairs(
        [(turn_id, thread_id) for turn_id, thread_id in turns], acted, closing | refused
    )


async def _sends(session: AsyncSession, world_id: UUID, checks: EngineChecks) -> None:
    """`send_network_request` results that weren't run, or refused its input."""
    rows = await session.execute(
        select(ChatMessage.role, ChatMessage.content_text)
        .where(ChatMessage.world_id == world_id)
        .order_by(ChatMessage.conversation_id, ChatMessage.seq)
    )
    sends: set[str] = set()
    for role, content_text in rows:
        blocks = objects(json.loads(content_text))
        if role == "assistant":
            sends |= {
                as_str(b.get("id")) or ""
                for b in blocks
                if b.get("type") == "tool_use" and b.get("name") == SEND_NETWORK_REQUEST
            }
            continue
        for block in blocks:
            if block.get("type") != "tool_result" or block.get("tool_use_id") not in sends:
                continue
            text = _result_text(block)
            checks.not_run_sends += text.startswith(_NOT_RUN)
            checks.missing_field_errors += text.startswith(_INVALID_SEND)


async def _drafts(session: AsyncSession, world_id: UUID, checks: EngineChecks) -> None:
    drafts = await repo.list_in_world(session, EscalationDraft, world_id)
    consult_drafts = set(
        await session.scalars(
            select(Escalation.id).where(
                Escalation.world_id == world_id, Escalation.kind == "consult"
            )
        )
    )
    for draft in drafts:
        if draft.escalation_id not in consult_drafts:
            continue
        checks.drafts += 1
        question = draft.short_question
        if not question:
            checks.drafts_without_short_question += 1
        elif len(question.split()) > SHORT_QUESTION_WORDS:
            checks.long_short_questions.append(question)
        if draft.route is not None and _route_without_why(draft.route):
            checks.routes_without_why += 1


async def _auto_sends(
    session: AsyncSession, world_id: UUID, names: Mapping[UUID, str], checks: EngineChecks
) -> None:
    all_cards = await repo.list_in_world(session, Card, world_id)
    cards = [card for card in all_cards if card.auto]
    screenings = await repo.list_in_world(session, Screening, world_id)
    fits: dict[tuple[UUID, UUID], str] = {(s.search_id, s.org_id): s.fit for s in screenings}
    checks.auto_sends = len(cards)
    per_turn = Counter(card.turn_id for card in cards)
    checks.auto_requests_per_turn = max(per_turn.values(), default=0)
    for card in cards:
        payload = OutreachCardPayload.model_validate(card.payload)
        rows = {row.org_id: row for row in payload.recipients}
        for org_id in payload.auto_send_recipients or []:
            name = names.get(org_id, str(org_id))
            row = rows.get(org_id)
            screened = any(
                fits.get((search_id, org_id)) in AUTO_SEND_FITS for search_id in payload.search_ids
            )
            if row is None or not row.suggested or not screened:
                checks.auto_send_breaches.append(
                    f"{name} on card {card.id}: "
                    + ("not suggested" if row is None or not row.suggested else "not screened")
                )
    auto = {card.id for card in cards}
    sent = [
        SentRequest(
            id=request.id,
            conversation_id=request.conversation_id,
            created_at=request.created_at,
            auto=request.card_id in auto,
            recipients=[row.org_id for row in recipients_of(request)],
        )
        for request in await repo.list_in_world(session, NetworkRequest, world_id)
        if request.conversation_id is not None
    ]
    checks.duplicate_auto_recipients = duplicate_auto_recipients(
        sent, await _thread_ends(session, world_id), names
    )


async def _thread_ends(session: AsyncSession, world_id: UUID) -> dict[tuple[UUID, UUID], datetime]:
    """When each ended thread stopped its request being in touch with the org, by
    (request, responder org): see `ended_at`. A seeded thread with no snapshot falls back
    to its row's `last_activity_at`."""
    ended = [
        thread
        for thread in await repo.list_in_world(session, Thread, world_id)
        if thread.state in TERMINAL_STATES
    ]
    rows = await session.execute(
        select(Event.entity_id, Event.entity_version, Event.payload).where(
            Event.world_id == world_id,
            Event.entity_type == Thread.entity_type,
            Event.entity_id.in_([thread.id for thread in ended]),
        )
    )
    snapshots = [
        (entity_id, version, as_object(payload.get("data")) or {})
        for entity_id, version, payload in rows
        if entity_id is not None and version is not None
    ]
    at = ended_at(snapshots)
    return {
        (thread.request_id, thread.responder_org_id): at.get(thread.id, thread.last_activity_at)
        for thread in ended
    }


# --- The rules ------------------------------------------------------------------------


def empty_end_turn_failed(call: EmptyEndTurn, sent_cards: Mapping[tuple[UUID, str], bool]) -> bool:
    """Whether the runtime failed the turn on this empty `end_turn` rather than ending it
    (3.13 E1b, `TurnContext.quiet_opening`): it was a run's first call (step 0, or the
    step a turn resumes at, which opens on its card's result alone), and the opening
    asked for an answer. A resumed turn's opening asks for none once its card was
    approved or sent without review, unless notices came with it; a representative's
    asks for none when it is the code-written "nothing new", or only its held messages
    declined without a reason. `sent_cards` maps each card's (turn, tool call) to whether
    it was approved."""
    texts = [
        as_str(block.get("text")) or "" for block in call.opening if block.get("type") == "text"
    ]
    results = [block for block in call.opening if block.get("type") == "tool_result"]
    if not texts and len(results) == 1:
        sent = sent_cards.get((call.turn_id, as_str(results[0].get("tool_use_id")) or ""))
        if sent is not None:
            return not sent or call.notices
    if call.step_idx > 0:
        return False
    if len(texts) == 1 and _NOTHING_NEW.fullmatch(texts[0]) is not None:
        return False
    # A held bare-text reply's decline answers no call, so it comes as text.
    declined = [*texts, *(_result_text(block) for block in results)]
    return not declined or any(text != HELD_DECLINED for text in declined)


def silent_turn_pairs(
    turns: Sequence[tuple[UUID, UUID | None]], acted: Set[UUID], ends: Set[UUID]
) -> int:
    """Pairs of back-to-back representative turns on a thread that sent nothing,
    consulted no one, held nothing and didn't close, as `ThreadWorkflow` counts them
    toward expiry (3.13 E3). `turns` is (turn, thread) in the order they ran; `acted`,
    the turns that did any of those things; `ends`, the turns that ended their thread
    (a close or a refusal). A refusal isn't a silent turn, and what follows a thread's
    end is its last turn, which only summarizes."""
    quiet_run: Counter[UUID | None] = Counter()
    ended: set[UUID | None] = set()
    pairs = 0
    for turn_id, thread_id in turns:
        if thread_id in ended:
            continue
        if turn_id in ends:
            ended.add(thread_id)
            continue
        if turn_id in acted:
            quiet_run[thread_id] = 0
            continue
        quiet_run[thread_id] += 1
        if quiet_run[thread_id] == 2:
            pairs += 1
            quiet_run[thread_id] = 0
    return pairs


def ended_at(
    snapshots: Iterable[tuple[UUID, int, Mapping[str, JsonValue]]],
) -> dict[UUID, datetime]:
    """Each thread's `last_activity_at` in its first terminal snapshot, from
    (thread, version, data) rows: the move into a terminal state stamps it, and a
    terminal thread never leaves its state. The row's own value is no use: the last
    side's summary stamps it again after the thread ended (`store_close_summary`)."""
    first: dict[UUID, tuple[int, datetime]] = {}
    for thread_id, version, data in snapshots:
        stamp = as_str(data.get("last_activity_at"))
        if data.get("state") not in TERMINAL_STATES or stamp is None:
            continue
        if thread_id not in first or version < first[thread_id][0]:
            first[thread_id] = (version, datetime.fromisoformat(stamp))
    return {thread_id: at for thread_id, (_, at) in first.items()}


def duplicate_auto_recipients(
    sent: Sequence[SentRequest],
    ended: Mapping[tuple[UUID, UUID], datetime],
    names: Mapping[UUID, str],
) -> list[str]:
    """Each recipient an auto request went to while an earlier request from its chat,
    reviewed or auto, was still in touch with it: sent there, its thread to it not ended
    (`ended`, by request and org) when the auto request was created. That is what the
    no-duplicates guard refuses (3.10); a follow-up after the thread ended isn't one."""
    chats: defaultdict[UUID, list[SentRequest]] = defaultdict(list)
    for request in sorted(sent, key=lambda r: (r.created_at, r.id)):
        chats[request.conversation_id].append(request)
    found: list[str] = []
    for chat in chats.values():
        for index, later in enumerate(chat):
            if not later.auto:
                continue
            for org_id in later.recipients:
                earlier = next(
                    (
                        request
                        for request in chat[:index]
                        if org_id in request.recipients
                        and ended.get((request.id, org_id), later.created_at) >= later.created_at
                    ),
                    None,
                )
                if earlier is not None:
                    found.append(
                        f"{names.get(org_id, str(org_id))}: request {later.id} while request "
                        f"{earlier.id} was still in touch"
                    )
    return found


def _closes(
    threads: Sequence[ThreadRow],
    messages: Sequence[Message],
    summaries: Mapping[tuple[UUID, str], str | None],
    names: Mapping[UUID, str],
    checks: EngineChecks,
) -> None:
    by_id = {thread.id: thread for thread in threads}
    for thread_id, thread_messages in _by_thread(messages).items():
        thread = by_id.get(thread_id)
        if thread is None:
            continue
        for close in (m for m in thread_messages if m.kind == "close"):
            side = "requester" if close.from_org_id == thread.requester_org_id else "responder"
            closer = names.get(close.from_org_id, "?")
            replied = any(
                m.kind == "message" and m.from_org_id != close.from_org_id and m.seq < close.seq
                for m in thread_messages
            )
            if not replied:
                checks.early_closes.append(f"{closer} closed before any reply ({thread_id})")
            summary = summaries.get((thread_id, side)) or ""
            asked = any(
                m.kind == "message"
                and m.from_org_id == close.from_org_id
                and m.turn_id is not None
                and m.turn_id == close.turn_id
                and m.body.rstrip().endswith("?")
                for m in thread_messages
            )
            if WAITING.search(summary) or asked:
                why = "asked a question in the same turn" if asked else f"summary: {summary}"
                checks.waiting_closes.append(f"{closer} ({thread_id}): {why}")


def _messages(
    threads: Sequence[ThreadRow],
    messages: Sequence[Message],
    names: Mapping[UUID, str],
    slugs: Mapping[UUID, str],
    scenario: str,
    checks: EngineChecks,
) -> None:
    by_id = {thread.id: thread for thread in threads}
    recipients: defaultdict[UUID, set[UUID]] = defaultdict(set)
    for thread in threads:
        recipients[thread.request_id].add(thread.responder_org_id)
    leaks = OPENER_LEAKS.get(scenario, {})
    sent = [m for m in messages if m.kind == "message"]
    by_turn: defaultdict[tuple[UUID, UUID], list[Message]] = defaultdict(list)
    for message in sent:
        if message.turn_id is not None:
            by_turn[(message.thread_id, message.turn_id)].append(message)
        if META_NOTE.search(message.body):
            checks.meta_notes.append(message.body)
    checks.multi_send_turns = [
        [m.body for m in turn[1:]] for turn in by_turn.values() if len(turn) > 1
    ]
    for thread_id, thread_messages in _by_thread(sent).items():
        thread = by_id.get(thread_id)
        if thread is None:
            continue
        own = [m for m in thread_messages if m.from_org_id == thread.requester_org_id]
        others = recipients[thread.request_id] - {thread.responder_org_id}
        for message in own:
            for org_id in others:
                if _names(message.body, names.get(org_id, "")):
                    checks.cross_mentions.append(
                        f"to {names.get(thread.responder_org_id, '?')}: names "
                        f"{names[org_id]}: {message.body}"
                    )
        # A request with no other recipient has no other part to give away, and its
        # requester may well name itself (Ridgeline's own seeded request to Northlake).
        pattern = leaks.get(slugs.get(thread.responder_org_id, ""))
        if pattern is not None and own and others:
            checks.openers_checked += 1
            if pattern.search(own[0].body):
                checks.opener_leaks.append(
                    f"to {names.get(thread.responder_org_id, '?')}: {own[0].body}"
                )


def opener_leak(scenario: str, recipient_slug: str, opener: str) -> str | None:
    """What a thread's first message to `recipient_slug` gave away of another
    recipient's part, if the scenario's patterns catch any."""
    pattern = OPENER_LEAKS.get(scenario, {}).get(recipient_slug)
    found = pattern.search(opener) if pattern is not None else None
    return found.group(0) if found else None


def _names(text: str, name: str) -> bool:
    """Whether `text` names `name` in full, as words, ignoring case."""
    return bool(name) and re.search(rf"(?i)(?<!\w){re.escape(name)}(?!\w)", text) is not None


def _route_without_why(route: Mapping[str, JsonValue]) -> bool:
    try:
        parsed = Route.model_validate(route)
    except ValidationError:
        return False
    return any(not assignee.why.strip() for assignee in parsed.assignees)


def _title(what: str, title: str | None) -> str:
    if not title:
        return f"{what}: (none)"
    return f"{what}: {title} ({len(title.split())} words)"


def _by_thread(messages: Iterable[Message]) -> dict[UUID, list[Message]]:
    grouped: defaultdict[UUID, list[Message]] = defaultdict(list)
    for message in messages:
        grouped[message.thread_id].append(message)
    return grouped


def _tool_results(content_text: str) -> str:
    return "\n".join(
        _result_text(block)
        for block in objects(json.loads(content_text))
        if block.get("type") == "tool_result"
    )


def _result_text(block: Mapping[str, JsonValue]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    return "".join(as_str(part.get("text")) or "" for part in objects(content))


# --- Formatting -----------------------------------------------------------------------


def engine_check_lines(checks: EngineChecks) -> list[str]:
    def listed(label: str, items: Sequence[str]) -> list[str]:
        return [f"  {label}: {len(items)}", *(f"    - {_one_line(item)}" for item in items)]

    lines = [
        *listed("early_closes", checks.early_closes),
        f"  close_refusals: {checks.close_refusals}",
        *listed("waiting_closes", checks.waiting_closes),
        f"  empty_turn_failures: {checks.empty_turn_failures}",
        f"  empty_end_turns_handled: {checks.empty_end_turns_handled}",
        f"  multi_send_turns: {len(checks.multi_send_turns)}",
        *(f"    - then: {_one_line(text)}" for later in checks.multi_send_turns for text in later),
        f"  silent_turn_pairs: {checks.silent_turn_pairs}",
        *listed("meta_notes", checks.meta_notes),
        *listed("cross_mentions", checks.cross_mentions),
        *listed(f"opener_leaks (of {checks.openers_checked} checked)", checks.opener_leaks),
        f"  not_run_sends: {checks.not_run_sends}",
        f"  missing_field_errors: {checks.missing_field_errors}",
        f"  titles: {len(checks.titles)}",
        *(f"    - {title}" for title in checks.titles),
        f"  drafts: {checks.drafts}; without short_question: "
        f"{checks.drafts_without_short_question}",
        *listed(f"short_question over {SHORT_QUESTION_WORDS} words", checks.long_short_questions),
        f"  routes_without_why: {checks.routes_without_why}",
        f"  auto_sends: {checks.auto_sends}",
        *listed("auto_send_breaches", checks.auto_send_breaches),
        f"  auto_requests_per_turn (max): {checks.auto_requests_per_turn}",
        *listed("duplicate_auto_recipients", checks.duplicate_auto_recipients),
    ]
    return lines


def _one_line(text: str, limit: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


__all__ = [
    "META_NOTE",
    "OPENER_LEAKS",
    "WAITING",
    "EngineChecks",
    "engine_check_lines",
    "engine_checks",
    "opener_leak",
]
