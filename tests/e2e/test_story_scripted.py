"""The suggested story end to end on `ScriptedModel` (REVISION_UNSTRUCTURED.md §10, §11).

Everything is real except the model: the API in process, Postgres (this test's
database), Temporal (a local dev server) with both task queues served by the worker
`harmony worker` builds, and the cassettes in tests/fixtures/cassettes. `harmony run`'s
player (`storyrun`) acts where people would: it sends the suggested message, approves
the outreach card and resolves escalations by the story's rules. The adversary's
request plays alongside: nothing starts it when the world is created, and it starts
when the story first acts as Helen at Northlake (LIVE_DEMO_PLAN 3.3). Helen settles its
escalation here, as she would from her inbox. On today's cassettes she then reopens it
after Vantage closes (FEEDBACK_R2_PLAN §1.9), and Jordan's Reopen after that is refused
by Vantage's guidance writer (REOPEN.md §10.4).

It plays twice: on today's cassettes, and with the answerers' conversations as P0–P2
recorded them (tests/fixtures/cassettes_p0_p2/), whose drafts route member slugs with
one shared reason and ask no short question, the shapes stored before the live phase
(LIVE_DEMO_PLAN §4, L0's compatibility check). The chat's send is today's either way:
the live phase requires its titles and asks (L8).

The run passes when every beat the story expects was seen and Claude has summed up the
closed request. The assertions then read what happened from the database and the event
log, not from the beats: who was asked what, what the sharing check said, what each
representative's requests held, and where each close summary went.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final, Literal
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.testing import WorkflowEnvironment

from harmony.agents.models import (
    AssistantTextItem,
    EscalationResolution,
    InlineQuestionItem,
    Route,
)
from harmony.api.routes.conversations import ConversationDetail
from harmony.api.routes.escalations import ResolveEscalation
from harmony.api.routes.threads import ReopenRequest
from harmony.config import Settings
from harmony.db import repo
from harmony.db.models import (
    AgentTurn,
    Card,
    Consult,
    Document,
    Escalation,
    EscalationDraft,
    Event,
    KnowledgeItem,
    ModelCall,
    NetworkRequest,
    RepNotice,
    RepSession,
    Steer,
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

type Answers = Literal["today", "p0_p2"]

SCENARIO: Final = "harmony-demo"
# The answerers' cassettes as P0–P2 recorded them, played in place of today's.
P0_P2_CASSETTES: Final = Path(__file__).parents[1] / "fixtures" / "cassettes_p0_p2"
STORY_TIMEOUT_S: Final = 240.0
# How long the adversary's conversation may take to reach Helen, and then to close.
ADVERSARY_TIMEOUT_S: Final = 120.0
# The seeded request's title for Northlake's side (threads.yaml).
ADVERSARY_TITLE: Final = "Rush CT slot enquiry"


def _sid(kind: SeedKind, slug: str) -> UUID:
    return seed_id(SCENARIO, kind, slug)


HENDERSON, NORTHLAKE, RIDGELINE, CALDER, VANTAGE = (
    _sid("org", slug) for slug in ("henderson", "northlake", "ridgeline", "calder", "vantage")
)
ALEX, HELEN, RAJ, JORDAN = (_sid("member", slug) for slug in ("alex", "helen", "raj", "jordan"))
ADVERSARY_REQUEST: Final = _sid("network_request", "vantage-northlake-thursday-ct")
# What Helen releases to Vantage instead of the draft, which named a client's booking.
HELENS_ANSWER: Final = (
    "We don't share who books our instruments. Thursday afternoons aren't available, but "
    "your client can have CT time on another weekday from Nov 3."
)
# What Northlake knows of the booking the adversary fishes for (its client record, and
# the draft the sharing check stopped). Vantage names Voltaic itself, in its forged line.
CLIENT_DETAILS: Final = ("confidentiality agreement", "standing CT booking", "NDA")
CLIENT: Final = "Voltaic"
# Helen's Reopen (FEEDBACK_R2_PLAN §1.9). Here she made no Edit first, so it is
# Northlake's first steer on the thread, and the cassettes write guidance v1 for it.
REOPEN_TEXT: Final = "Offer them our general capabilities sheet instead."
GUIDANCE_BLOCK: Final = '<guidance from="Northlake Characterization Core" version="1">'
GUIDANCE: Final = "Say only whether a slot is available."
REOPENED_BY_NORTHLAKE: Final = "Northlake Characterization Core reopened this conversation"
# Jordan's Reopen, which Vantage's guidance writer refuses.
JORDANS_TEXT: Final = "Ask them again who books Thursdays."


@pytest.mark.parametrize("answers", ["today", "p0_p2"])
async def test_the_suggested_story_plays_and_every_firewall_holds(
    answers: Answers,
    app_settings: Settings,
    db_engine: AsyncEngine,
    db_sessionmaker: Sessionmaker,
    temporal_env: WorkflowEnvironment,
    tmp_path: Path,
) -> None:
    # Scripted always: this test never calls the model API.
    settings = app_settings.model_copy(
        update={
            "model_mode": "scripted",
            "admin_secret": SecretStr("e2e-admin-secret"),
            "cassette_dir": _cassettes(answers, app_settings.cassette_dir, tmp_path),
        }
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
            reopened = (
                await _reopen_the_adversary(db_sessionmaker, api, world_id, adversary)
                if answers == "today"
                else None
            )
            assert result.request_id is not None
            world = await _read_world(db_sessionmaker, world_id, result.request_id)
        finally:
            if player.world_id is not None:
                await retirer.retire(player.world_id)

    summary = chat.items[-1]
    assert isinstance(summary, AssistantTextItem), summary
    assert "$1,759" in summary.markdown
    _assert_the_outreach(world)
    _assert_no_one_was_needed_before_the_approval(world)
    _assert_ridgeline_settled_from_the_records(world)
    _assert_northlake_quoted_from_what_it_publishes(world)
    _assert_alexs_review(world, chat)
    _assert_the_close_summaries(world)
    _assert_raj_never_got_access(world)
    _assert_the_firewall(world)
    _assert_the_adversary(world, adversary)
    if reopened is not None:
        _assert_the_reopen(world, reopened)
    _assert_the_drafts(world, answers)
    _assert_agent_turns(world, reopened=reopened is not None)


def _cassettes(answers: Answers, today: Path, tmp_path: Path) -> Path:
    """Today's cassette directory, or a copy with the answerers' cassettes swapped for
    P0–P2's."""
    if answers == "today":
        return today
    played = tmp_path / "cassettes"
    shutil.copytree(today, played)
    for recorded in P0_P2_CASSETTES.glob("*.yaml"):
        assert (played / recorded.name).is_file(), recorded.name
        shutil.copy(recorded, played / recorded.name)
    return played


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
    inbox = await api.inbox()
    buckets = (
        inbox.needs_you,
        inbox.with_your_team,
        inbox.claude_handling,
        inbox.shared_with_you,
        inbox.done,
    )
    titles = {item.title for bucket in buckets for item in bucket if item.thread_id == thread_id}
    assert titles == {ADVERSARY_TITLE}
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


@dataclass(frozen=True, slots=True)
class Reopened:
    helens: Steer
    jordans: Steer
    # The adversary's request after both attempts.
    request: NetworkRequest
    # Northlake's notices on the thread, and Helen's view of it, before and after
    # Jordan's refused attempt.
    northlake_notices: tuple[int, int]
    helens_view: tuple[object, object]


async def _reopen_the_adversary(
    sm: Sessionmaker, api: HarmonyApi, world_id: UUID, thread_id: UUID
) -> Reopened:
    """Helen reopens the closed conversation and it runs to its next close; then Jordan
    tries, and Vantage's guidance writer refuses."""

    async def may_reopen() -> bool | None:
        detail = await api.thread(thread_id)
        return True if detail.reopen is not None and detail.reopen.state == "allowed" else None

    async def steer_of(steer_id: UUID) -> Steer | None:
        async with sm() as session:
            steer = await repo.get_required(session, Steer, world_id, steer_id)
        return steer if steer.status != "working" else None

    async def run_2_ended() -> bool | None:
        async with sm() as session:
            thread = await repo.get_required(session, Thread, world_id, thread_id)
            northlake = await session.get(RepSession, (world_id, thread_id, "responder"))
        summarized = northlake is not None and northlake.close_summary is not None
        return True if thread.run == 2 and thread.run_finished_at and summarized else None

    async def northlake_notices() -> int:
        async with sm() as session:
            rows = await session.scalars(
                select(RepNotice.seq).where(
                    RepNotice.world_id == world_id,
                    RepNotice.thread_id == thread_id,
                    RepNotice.side == "responder",
                )
            )
            return len(rows.all())

    async def helens_view() -> object:
        await api.act_as(HELEN)
        detail = await api.thread(thread_id)
        inbox = await api.inbox()
        buckets = (
            inbox.needs_you,
            inbox.with_your_team,
            inbox.claude_handling,
            inbox.shared_with_you,
            inbox.done,
        )
        items = [item for bucket in buckets for item in bucket if item.thread_id == thread_id]
        return (detail.state, detail.run, detail.runs, detail.messages, items)

    await api.act_as(HELEN)
    await _eventually(may_reopen, "Northlake may reopen the adversary's conversation")
    body = ReopenRequest(client_key=uuid4(), as_member_id=HELEN, body=REOPEN_TEXT)
    helens = (await api.reopen(thread_id, body)).steer_id
    await _eventually(run_2_ended, "the reopened conversation closed again")

    before = (await northlake_notices(), await helens_view())
    await api.act_as(JORDAN)
    await _eventually(may_reopen, "Vantage may reopen the conversation")
    body = ReopenRequest(client_key=uuid4(), as_member_id=JORDAN, body=JORDANS_TEXT)
    jordans = (await api.reopen(thread_id, body)).steer_id
    jordans_steer = await _eventually(lambda: steer_of(jordans), "Jordan's Reopen settled")
    after = (await northlake_notices(), await helens_view())
    async with sm() as session:
        helens_steer = await repo.get_required(session, Steer, world_id, helens)
        request = await repo.get_required(session, NetworkRequest, world_id, ADVERSARY_REQUEST)
    return Reopened(
        helens=helens_steer,
        jordans=jordans_steer,
        request=request,
        northlake_notices=(before[0], after[0]),
        helens_view=(before[1], after[1]),
    )


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
    drafts: list[EscalationDraft]
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
        drafts = await rows(EscalationDraft)
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
        drafts=drafts,
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


def _assert_no_one_was_needed_before_the_approval(world: World) -> None:
    """Nothing model-driven runs when the world is created: the Vantage probe starts
    once the story acts as Helen, after Alex approves the outreach, so the demo bar
    asks for nobody before then."""
    [approved] = [
        e.seq
        for e in world.events
        if e.entity_type == "card" and '"status": "approved"' in json.dumps(e.payload)
    ]
    needs_human = [e.seq for e in world.events if e.type == "demo.needs_human"]
    assert needs_human
    assert min(needs_human) > approved
    adversary = [e.seq for e in world.events if e.thread_id == world.adversary.id]
    assert adversary
    assert min(adversary) > approved


def _assert_ridgeline_settled_from_the_records(world: World) -> None:
    """Ridgeline's questions were answered from Henderson Frame Lab's records within what it
    shares, Ridgeline quoted from its price list, and Henderson Frame Lab's representative
    accepted the list price on its own: nobody on either side was asked."""
    ridgeline = world.threads[RIDGELINE]
    assert not [e for e in world.escalations if e.thread_id == ridgeline.id]
    [details] = world.consults_on(ridgeline, HENDERSON)
    assert details.sharing_check is not None
    assert details.sharing_check["within_sharing"] is True
    assert details.released_by == "auto"
    assert details.released_text is not None
    assert "Xradia 620 Versa" in details.released_text
    assert "carbon-fiber" in details.released_text
    [quote] = world.consults_on(ridgeline, RIDGELINE)
    assert quote.released_by == "auto"
    assert any("$325" in body for body in _said(world, ridgeline, RIDGELINE))


def _assert_northlake_quoted_from_what_it_publishes(world: World) -> None:
    """Northlake's answer came from its public calendar and price list, so the sharing
    check passed and nobody at Northlake was asked. It was released to the Henderson
    conversation only, and who freed the Nov 4 block stayed out."""
    northlake = world.threads[NORTHLAKE]
    assert not [
        e for e in world.escalations if e.thread_id == northlake.id and e.org_id == NORTHLAKE
    ]
    [consult] = world.consults_on(northlake, NORTHLAKE)
    assert consult.sharing_check is not None
    assert consult.sharing_check["within_sharing"] is True
    assert (consult.released_by, consult.released_by_member_id) == ("auto", None)
    released = consult.released_text
    assert released is not None
    assert "$1,284" in released
    assert "$1,434" in released
    assert CLIENT not in released
    # Released to this conversation only: Northlake's representative on this thread
    # read it, no other representative did, and Northlake's sharing didn't change.
    assert any(released in s for s in world.rep_requests[(NORTHLAKE, northlake.id)])
    for (org_id, thread_id), strings in world.rep_requests.items():
        if (org_id, thread_id) != (NORTHLAKE, northlake.id):
            assert not any(released in s for s in strings), (org_id, thread_id)
    assert not [
        t for t in world.trusted if t.org_id == NORTHLAKE and t.counterparty_org_id == HENDERSON
    ]
    northlake_said = _said(world, northlake, NORTHLAKE)
    assert any("$1,434" in body for body in northlake_said)
    assert not any(CLIENT in body for body in northlake_said)


def _assert_alexs_review(world: World, chat: ConversationDetail) -> None:
    """Henderson Frame Lab's details went to Northlake from its records; Northlake's quote was
    over the $1,000 its representative may commit alone, so it asked Alex in the chat.
    He approved the recommendation as drafted, the only time anyone on the story's
    request was asked, and the representative booked it."""
    story = {thread.id for thread in world.threads.values()}
    [escalation] = [e for e in world.escalations if e.thread_id in story]
    northlake = world.threads[NORTHLAKE]
    details, review = world.consults_on(northlake, HENDERSON)
    assert details.released_by == "auto"
    assert world.escalation_of(review) == escalation
    assert escalation.assignees == [str(ALEX)]
    assert _needs_human(world, escalation) == ["alex"]
    assert _resolution(escalation) == ("approve", None, ALEX)
    assert review.released_by == "member"
    released = review.released_text
    assert released is not None
    assert "priority" in released
    assert "$1,434" in released
    inline = [
        i
        for i in chat.items
        if isinstance(i, InlineQuestionItem) and i.escalation_id == escalation.id
    ]
    assert [(i.from_org.id, i.status) for i in inline] == [(NORTHLAKE, "resolved")]
    assert any("priority" in body for body in _said(world, northlake, HENDERSON))


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
    assert "Raj's note, Nov 4 compression block" in {title for title, _ in world.record_bodies}
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


def _assert_the_reopen(world: World, reopened: Reopened) -> None:
    """Helen's text reached only her side's guidance writer: Northlake's representative
    got the guidance block, Vantage only that Northlake reopened. Jordan's refused
    attempt changed nothing Northlake can see, and the request ended its second round."""
    thread = world.adversary
    assert (thread.run, thread.state, thread.end_reason, thread.closed_by) == (
        2,
        "closed",
        "closed",
        "requester",
    )
    assert reopened.helens.status == "done"
    assert (reopened.jordans.status, reopened.jordans.failure_reason) == ("failed", "refused")
    northlake = world.rep_requests[(NORTHLAKE, thread.id)]
    vantage = world.rep_requests[(VANTAGE, thread.id)]
    assert any(GUIDANCE_BLOCK in s for s in northlake)
    for strings in (northlake, vantage):
        assert not any(REOPEN_TEXT in s or JORDANS_TEXT in s for s in strings)
    assert not any(GUIDANCE_BLOCK in s or GUIDANCE in s for s in vantage)
    assert any(REOPENED_BY_NORTHLAKE in s for s in vantage)
    assert any("general capabilities sheet" in body for body in _said(world, thread, NORTHLAKE))
    assert reopened.northlake_notices[0] == reopened.northlake_notices[1]
    assert reopened.helens_view[0] == reopened.helens_view[1]
    assert (reopened.request.status, reopened.request.round) == ("closed", 2)


def _assert_the_drafts(world: World, answers: Answers) -> None:
    """Each consult's drafts came in the cassettes' shape and were stored in today's:
    every assignee has a why (a P0–P2 route's one reason, else its own), and only
    today's drafts ask a short question."""
    consults = {e.id for e in world.escalations if e.kind == "consult"}
    drafts = [d for d in world.drafts if d.escalation_id in consults]
    assert drafts
    for draft in drafts:
        assert (draft.short_question is None) == (answers == "p0_p2"), draft.escalation_id
        assert draft.route is not None
        route = Route.model_validate(draft.route)
        assert route.model_dump(mode="json") == draft.route
        assert all(assignee.why for assignee in route.assignees)


def _assert_agent_turns(world: World, *, reopened: bool) -> None:
    roles = {turn.role for turn in world.agent_turns}
    played = {"internal", "representative", "answerer", "sharing_check", "screener"}
    assert roles == played | ({"steer"} if reopened else set())
    assert all(t.latency_ms is not None and t.completed_at is not None for t in world.agent_turns)
    reps = {(t.org_id, t.thread_id) for t in world.agent_turns if t.role == "representative"}
    northlake = world.threads[NORTHLAKE].id
    assert {(HENDERSON, northlake), (NORTHLAKE, northlake), (VANTAGE, world.adversary.id)} <= reps
