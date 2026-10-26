"""The suggested story end to end on `ScriptedModel` (REVISION_UNSTRUCTURED.md §10, §11).

Everything is real except the model: the API in process, Postgres (this test's
database), Temporal (a local dev server) with both task queues served by the worker
`harmony worker` builds, and the cassettes in tests/fixtures/cassettes. `harmony run`'s
player (`storyrun`) acts where people would: it sends the suggested message, approves
the outreach card and resolves escalations by the story's rules. The adversary's
request, which the world opens when it is created, plays alongside; Helen settles its
escalation here, as she would from her inbox.

The run passes when every beat the story expects was seen and Claude has summed up the
closed request. The assertions then read what happened from the database and the event
log, not from the beats: who was asked what, what the sharing check said, what each
representative's requests held, and where each close summary went.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.testing import WorkflowEnvironment

from harmony.agents.models import AssistantTextItem, EscalationResolution, InlineQuestionItem
from harmony.api.routes.conversations import ConversationDetail
from harmony.api.routes.escalations import ResolveEscalation
from harmony.config import Settings
from harmony.db import repo
from harmony.db.models import (
    AgentTurn,
    Card,
    Consult,
    Document,
    Escalation,
    Event,
    KnowledgeItem,
    ModelCall,
    NetworkRequest,
    RepSession,
    Thread,
    ThreadAccess,
    ThreadMessage,
    TrustedOrganization,
)
from harmony.db.models.base import WorldScoped
from harmony.domain import cards, requests
from harmony.seeds.ids import SeedKind, seed_id
from harmony.seeds.loader import load_scenario
from harmony.storyrun.api import HarmonyApi, in_process
from harmony.storyrun.decide import StoryDecisions
from harmony.storyrun.play import StoryPlayer
from harmony.storyrun.worker import running_worker
from harmony.temporal.worker import WorkerDeps, model_runtime
from harmony.worlds.retirer_temporal import TemporalRetirer

type Sessionmaker = async_sessionmaker[AsyncSession]

SCENARIO: Final = "harmony-demo"
STORY_TIMEOUT_S: Final = 240.0
# How long the adversary's conversation may take to reach Helen, and then to close.
ADVERSARY_TIMEOUT_S: Final = 120.0


def _sid(kind: SeedKind, slug: str) -> UUID:
    return seed_id(SCENARIO, kind, slug)


HENDERSON, NORTHLAKE, RIDGELINE, CALDER, VANTAGE = (
    _sid("org", slug) for slug in ("henderson", "northlake", "ridgeline", "calder", "vantage")
)
ALEX, HELEN, RAJ = (_sid("member", slug) for slug in ("alex", "helen", "raj"))
ADVERSARY_REQUEST: Final = _sid("network_request", "vantage-northlake-thursday-sem")
# What Helen releases to Vantage instead of the draft, which named a client's booking.
HELENS_ANSWER: Final = (
    "We don't share who books our instruments. Thursday afternoons aren't available, but "
    "your client can have SEM time on another weekday from Nov 3."
)
# What Northlake knows of the booking the adversary fishes for (its client record, and
# the draft the sharing check stopped). Vantage names Voltaic itself, in its forged line.
CLIENT_DETAILS: Final = ("confidentiality agreement", "standing SEM booking", "NDA")
CLIENT: Final = "Voltaic"


async def test_the_suggested_story_plays_and_every_firewall_holds(
    app_settings: Settings,
    db_engine: AsyncEngine,
    db_sessionmaker: Sessionmaker,
    temporal_env: WorkflowEnvironment,
) -> None:
    # Scripted always: this test never calls the model API.
    settings = app_settings.model_copy(
        update={"model_mode": "scripted", "admin_secret": SecretStr("e2e-admin-secret")}
    )
    scenario = load_scenario(SCENARIO)
    story = scenario.story
    assert story is not None
    client = temporal_env.client
    lines: list[str] = []
    runtime = model_runtime(settings, db_sessionmaker)
    retirer = TemporalRetirer(client, db_sessionmaker, row_ttl=timedelta(0))
    async with (
        running_worker(WorkerDeps(settings, client, db_sessionmaker, runtime)) as serving,
        in_process(settings, engine=db_engine, temporal=client, retirer=retirer) as api,
    ):
        player = StoryPlayer(
            api,
            scenario,
            story,
            StoryDecisions(story),
            log=lines.append,
            timeout_s=STORY_TIMEOUT_S,
            worker=serving.serving,
            failures=serving.log.failures,
        )
        try:
            result = await player.play()
            log = "\n".join(lines)
            assert result.outcome == "done", f"{result.outcome}: {result.detail}\n{log}"
            assert result.complete, result.missing()
            assert await player.settle(result), "the request closed and Claude summed it up"
            chat, _ = await player.transcripts(result)
            assert chat is not None
            world_id = player.world_id
            assert world_id is not None
            adversary = await _play_the_adversary(db_sessionmaker, api, world_id)
            assert result.request_id is not None
            world = await _read_world(db_sessionmaker, world_id, result.request_id)
        finally:
            if player.world_id is not None:
                await retirer.retire(player.world_id)

    summary = chat.items[-1]
    assert isinstance(summary, AssistantTextItem), summary
    assert "$1,735" in summary.markdown
    _assert_the_outreach(world)
    _assert_ridgelines_question(world, chat)
    _assert_northlakes_consult(world)
    _assert_the_go_aheads(world, chat)
    _assert_the_close_summaries(world)
    _assert_raj_never_got_access(world)
    _assert_the_firewall(world)
    _assert_the_adversary(world, adversary)
    _assert_agent_turns(world)


# --- The adversary ----------------------------------------------------------------------


async def _play_the_adversary(sm: Sessionmaker, api: HarmonyApi, world_id: UUID) -> UUID:
    """Helen settles Vantage's escalation, then the thread runs to its close. Returns
    the thread."""

    async def escalated() -> Escalation | None:
        async with sm() as session:
            return await session.scalar(
                repo.in_world(Escalation, world_id)
                .join(
                    Thread,
                    (Thread.world_id == Escalation.world_id) & (Thread.id == Escalation.thread_id),
                )
                .where(Thread.request_id == ADVERSARY_REQUEST, Escalation.status == "open")
            )

    escalation = await _eventually(escalated, "Vantage's probe reached Northlake's inbox")
    thread_id = escalation.thread_id
    assert thread_id is not None
    await api.act_as(HELEN)
    await api.resolve(
        escalation.id,
        ResolveEscalation(
            action="approve", text=HELENS_ANSWER, draft_rev=escalation.current_draft_rev
        ),
    )

    async def closed() -> bool | None:
        async with sm() as session:
            thread = await repo.get_required(session, Thread, world_id, thread_id)
            summaries = await session.scalars(
                select(RepSession.close_summary).where(
                    RepSession.world_id == world_id, RepSession.thread_id == thread.id
                )
            )
            both = len([summary for summary in summaries if summary]) == 2
        return True if thread.state == "closed" and both else None

    await _eventually(closed, "the adversary's conversation closed with both summaries")
    return thread_id


async def _eventually[T](probe: Callable[[], Awaitable[T | None]], what: str) -> T:
    deadline = time.monotonic() + ADVERSARY_TIMEOUT_S
    while time.monotonic() < deadline:
        found = await probe()
        if found:
            return found
        await asyncio.sleep(0.25)
    raise AssertionError(f"timed out waiting until {what}")


# --- What the world holds ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class World:
    world_id: UUID
    cards: list[Card]
    request: NetworkRequest
    status: requests.RequestStatusView
    threads: dict[UUID, Thread]  # by responder org, the story's request only
    adversary: Thread
    messages: dict[UUID, list[ThreadMessage]]  # by thread
    consults: list[Consult]
    escalations: list[Escalation]
    summaries: dict[tuple[UUID, str], str]  # (thread, side) -> close summary
    access: list[ThreadAccess]
    events: list[Event]
    # Every string of every representative request, by (org, thread).
    rep_requests: dict[tuple[UUID, UUID], list[str]]
    # Every org's knowledge and document bodies, as (title, body).
    record_bodies: list[tuple[str, str]]
    trusted: list[TrustedOrganization]
    agent_turns: list[AgentTurn]

    def consults_on(self, thread: Thread, org_id: UUID) -> list[Consult]:
        return [c for c in self.consults if c.thread_id == thread.id and c.org_id == org_id]

    def escalation_of(self, consult: Consult) -> Escalation:
        return next(e for e in self.escalations if e.consult_id == consult.id)


async def _read_world(sm: Sessionmaker, world_id: UUID, request_id: UUID) -> World:
    async with sm() as session:

        async def rows[M: WorldScoped](model: type[M]) -> list[M]:
            return list(await repo.list_in_world(session, model, world_id))

        card_rows = await rows(Card)
        request = await repo.get_required(session, NetworkRequest, world_id, request_id)
        status = await requests.status(session, request)
        all_threads = await rows(Thread)
        threads = {t.responder_org_id: t for t in all_threads if t.request_id == request.id}
        [adversary] = [t for t in all_threads if t.request_id == ADVERSARY_REQUEST]
        messages: dict[UUID, list[ThreadMessage]] = defaultdict(list)
        for message in sorted(await rows(ThreadMessage), key=lambda m: m.seq):
            messages[message.thread_id].append(message)
        consults = sorted(await rows(Consult), key=lambda c: c.created_at)
        escalations = sorted(await rows(Escalation), key=lambda e: e.created_at)
        summaries = {
            (rep.thread_id, rep.side): rep.close_summary
            for rep in await rows(RepSession)
            if rep.close_summary
        }
        access = await rows(ThreadAccess)
        events = list(
            await session.scalars(
                select(Event).where(Event.world_id == world_id).order_by(Event.seq)
            )
        )
        turns = await rows(AgentTurn)
        reps = {turn.id: turn for turn in turns if turn.role == "representative"}
        rep_requests: dict[tuple[UUID, UUID], list[str]] = defaultdict(list)
        calls = await session.scalars(
            select(ModelCall).where(
                ModelCall.world_id == world_id, ModelCall.turn_id.in_(list(reps))
            )
        )
        for call in calls:
            turn = reps[call.turn_id]
            assert turn.thread_id is not None
            rep_requests[(turn.org_id, turn.thread_id)] += _strings(json.loads(call.request_text))
        bodies = [(item.title, item.body) for item in await rows(KnowledgeItem)]
        bodies += [(doc.title, doc.text) for doc in await rows(Document) if doc.text.strip()]
        trusted = await rows(TrustedOrganization)
    return World(
        world_id=world_id,
        cards=card_rows,
        request=request,
        status=status,
        threads=threads,
        adversary=adversary,
        messages=messages,
        consults=consults,
        escalations=escalations,
        summaries=summaries,
        access=access,
        events=events,
        rep_requests=rep_requests,
        record_bodies=bodies,
        trusted=trusted,
        agent_turns=turns,
    )


def _strings(value: object) -> Iterator[str]:
    """Every string in a request body: the system prompt, messages, tool results."""
    match value:
        case str():
            yield value
        case dict():
            for item in value.values():  # pyright: ignore[reportUnknownVariableType]
                yield from _strings(item)
        case list():
            for item in value:  # pyright: ignore[reportUnknownVariableType]
                yield from _strings(item)
        case _:
            pass


def _said(world: World, thread: Thread, org_id: UUID) -> list[str]:
    return [m.body for m in world.messages[thread.id] if m.from_org_id == org_id]


def _resolution(escalation: Escalation) -> tuple[str, str | None, UUID | None]:
    """What the member decided, what they said, and who they were."""
    assert escalation.resolution is not None, f"escalation {escalation.id} is unresolved"
    resolution = EscalationResolution.model_validate(escalation.resolution)
    return resolution.action, resolution.text, escalation.resolved_by_member_id


def _needs_human(world: World, escalation: Escalation) -> list[str]:
    """Who the demo bar was told the escalation needs."""
    return [
        str(e.payload.get("member_slug"))
        for e in world.events
        if e.type == "demo.needs_human" and e.payload.get("entity_id") == str(escalation.id)
    ]


# --- Assertions ---------------------------------------------------------------------------


def _assert_the_outreach(world: World) -> None:
    """Alex approved the card for Northlake and Ridgeline; Calder stays Screened."""
    [card] = world.cards
    assert card.status == "approved"
    resolution = cards.resolution_of(card)
    assert resolution is not None
    assert (resolution.decision, resolution.member_id) == ("approve", ALEX)
    proposed = {recipient.org_id for recipient in cards.payload_of(card).recipients}
    assert {NORTHLAKE, RIDGELINE, CALDER} <= proposed
    assert set(world.threads) == {NORTHLAKE, RIDGELINE}
    assert world.request.card_id == card.id
    rows = {row.org.id: row for row in world.status.recipients}
    assert (rows[CALDER].participant, rows[CALDER].thread_id) == ("screened", None)
    assert {rows[org].participant for org in (NORTHLAKE, RIDGELINE)} == {"closed"}
    approved = [
        e
        for e in world.events
        if e.entity_type == "card" and '"status": "approved"' in json.dumps(e.payload)
    ]
    assert [(e.scope_kind, e.member_id) for e in approved] == [("member", ALEX)]


def _assert_ridgelines_question(world: World, chat: ConversationDetail) -> None:
    """Ridgeline's question reached Alex in the chat; his answer went back and Ridgeline
    quoted."""
    ridgeline = world.threads[RIDGELINE]
    seal, _ = world.consults_on(ridgeline, HENDERSON)
    escalation = world.escalation_of(seal)
    assert escalation.assignees == [str(ALEX)]
    assert _needs_human(world, escalation) == ["alex"]
    action, text, by = _resolution(escalation)
    assert (action, by) == ("approve", ALEX)
    assert text is not None
    assert text.startswith("Kalrez, please.")
    assert seal.released_by == "member"
    assert seal.released_text is not None
    assert "Kalrez" in seal.released_text
    inline = [
        i
        for i in chat.items
        if isinstance(i, InlineQuestionItem) and i.escalation_id == escalation.id
    ]
    assert [(i.from_org.id, i.status, i.answer) for i in inline] == [(RIDGELINE, "resolved", text)]
    assert any("$325" in body for body in _said(world, ridgeline, RIDGELINE))


def _assert_northlakes_consult(world: World) -> None:
    """Northlake's draft failed the sharing check and went to Helen with that reason;
    her approval released it to the Henderson conversation only."""
    northlake = world.threads[NORTHLAKE]
    [consult] = world.consults_on(northlake, NORTHLAKE)
    assert consult.sharing_check is not None
    assert consult.sharing_check["within_sharing"] is False
    reason = str(consult.sharing_check["reason"])
    assert "reserve" in reason.lower()
    escalation = world.escalation_of(consult)
    assert escalation.assignees == [str(HELEN)]
    assert _needs_human(world, escalation) == ["helen"]
    action, _, by = _resolution(escalation)
    assert (action, by) == ("approve", HELEN)
    assert (consult.released_by, consult.released_by_member_id) == ("member", HELEN)
    released = consult.released_text
    assert released is not None
    # Released to this conversation only: Northlake's representative on this thread
    # read it, no other representative did, and Northlake's sharing didn't change.
    assert any(released in s for s in world.rep_requests[(NORTHLAKE, northlake.id)])
    for (org_id, thread_id), strings in world.rep_requests.items():
        if (org_id, thread_id) != (NORTHLAKE, northlake.id):
            assert not any(released in s for s in strings), (org_id, thread_id)
    assert not [
        t for t in world.trusted if t.org_id == NORTHLAKE and t.counterparty_org_id == HENDERSON
    ]
    assert any("$1,410" in body for body in _said(world, northlake, NORTHLAKE))


def _assert_the_go_aheads(world: World, chat: ConversationDetail) -> None:
    """Henderson's representative checked with Alex before agreeing, once per thread."""
    for org_id, expected in (
        (NORTHLAKE, "Yes, go ahead with Northlake."),
        (RIDGELINE, "Yes, go ahead with Ridgeline."),
    ):
        thread = world.threads[org_id]
        go_ahead = world.consults_on(thread, HENDERSON)[-1]
        escalation = world.escalation_of(go_ahead)
        assert escalation.assignees == [str(ALEX)]
        assert _resolution(escalation) == ("approve", expected, ALEX)
        assert go_ahead.released_text is not None
        assert expected in go_ahead.released_text
        inline = [
            i
            for i in chat.items
            if isinstance(i, InlineQuestionItem) and i.escalation_id == escalation.id
        ]
        assert [(i.from_org.id, i.status) for i in inline] == [(org_id, "resolved")]


def _assert_the_close_summaries(world: World) -> None:
    """Both threads closed, each side with its own summary, which never reached the
    other side: not as a message, not in its representative's requests, not in an
    event it could receive."""
    for org_id, thread in world.threads.items():
        assert thread.state == "closed"
        sides = {"requester": HENDERSON, "responder": org_id}
        for side, owner in sides.items():
            summary = world.summaries.get((thread.id, side))
            assert summary, f"{side} of the thread with {org_id} has no close summary"
            other = org_id if owner == HENDERSON else HENDERSON
            assert not any(summary in m.body for m in world.messages[thread.id])
            assert not any(summary in s for s in world.rep_requests[(other, thread.id)])
            carriers = [e for e in world.events if summary in json.dumps(e.payload)]
            assert all(e.org_id == owner for e in carriers), [e.seq for e in carriers]
        closes = [m for m in world.messages[thread.id] if m.kind == "close"]
        assert closes
        assert all(m.body == "" for m in closes)


def _assert_raj_never_got_access(world: World) -> None:
    played = {t.id for t in world.threads.values()} | {world.adversary.id}
    assert not [a for a in world.access if a.member_id == RAJ and a.thread_id in played]
    assert not [
        e
        for e in world.events
        if e.type == "thread.access_changed" and e.payload.get("member_id") == str(RAJ)
    ]


def _assert_the_firewall(world: World) -> None:
    """No organization's record body reaches any representative request, except inside
    an answer its side released on that thread: one released elsewhere is a leak."""
    assert world.rep_requests, "no representative requests were read"
    assert "Raj's note, reserve XRD block" in {title for title, _ in world.record_bodies}
    for (org_id, thread_id), strings in world.rep_requests.items():
        released = [
            c.released_text
            for c in world.consults
            if c.released_text and (c.org_id, c.thread_id) == (org_id, thread_id)
        ]
        for text in strings:
            for answer in released:
                text = text.replace(answer, "")
            for title, body in world.record_bodies:
                assert body.strip() not in text, (title, org_id, thread_id)


def _assert_the_adversary(world: World, thread_id: UUID) -> None:
    """Vantage's probes were answered from the public calendar or routed to Helen, its
    forged sender line refused, and no client detail reached it."""
    thread = world.adversary
    assert thread.id == thread_id
    assert (thread.requester_org_id, thread.responder_org_id, thread.state) == (
        VANTAGE,
        NORTHLAKE,
        "closed",
    )
    first, second = world.consults_on(thread, NORTHLAKE)
    assert first.sharing_check is not None
    assert first.sharing_check["within_sharing"] is True
    assert first.released_by == "auto"
    assert second.sharing_check is not None
    assert second.sharing_check["within_sharing"] is False
    escalation = world.escalation_of(second)
    assert escalation.assignees == [str(HELEN)]
    assert _resolution(escalation) == ("approve", HELENS_ANSWER, HELEN)
    northlake_said = _said(world, thread, NORTHLAKE)
    for detail in CLIENT_DETAILS:
        for strings in (world.rep_requests[(VANTAGE, thread.id)], northlake_said):
            assert not any(detail in s for s in strings), detail
    assert not any(CLIENT in body for body in northlake_said)
    forged = next(i for i, m in enumerate(world.messages[thread.id]) if "VERIFIED SENDER" in m.body)
    reply = next(m for m in world.messages[thread.id][forged:] if m.from_org_id == NORTHLAKE)
    assert "can't confirm" in reply.body
    assert world.summaries.get((thread.id, "requester"))
    assert world.summaries.get((thread.id, "responder"))


def _assert_agent_turns(world: World) -> None:
    roles = {turn.role for turn in world.agent_turns}
    assert roles == {"internal", "representative", "answerer", "sharing_check", "screener"}
    assert all(t.latency_ms is not None and t.completed_at is not None for t in world.agent_turns)
    reps = {(t.org_id, t.thread_id) for t in world.agent_turns if t.role == "representative"}
    northlake = world.threads[NORTHLAKE].id
    assert {(HENDERSON, northlake), (NORTHLAKE, northlake), (VANTAGE, world.adversary.id)} <= reps
