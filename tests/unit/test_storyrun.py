"""`harmony run`'s pieces without a server: the play loop against a fake API that
plays the suggested story as the backend's contract says it would, the story's
decisions and recasting, the report, the worker's activity-failure log, the poller
check, and the command's setup errors."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import JsonValue
from temporalio.api.taskqueue.v1 import PollerInfo
from temporalio.api.workflowservice.v1 import DescribeTaskQueueResponse
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from typer.testing import CliRunner

from harmony.agents.consult import HELD_DECLINED
from harmony.agents.models import (
    AssistantTextItem,
    CardRecipient,
    ChatItem,
    InlineQuestionItem,
    MemberRef,
    OutreachCardItem,
    UserTextItem,
)
from harmony.agents.tools.rep import CONSULT_AFTER_SEND
from harmony.api.routes.cards import ApproveCard
from harmony.api.routes.conversations import ConversationCreated, ConversationDetail
from harmony.api.routes.escalations import ResolveEscalation
from harmony.api.routes.inbox import Inbox, InboxEscalation, InboxItem
from harmony.api.routes.threads import ThreadDetail, ThreadMessageView
from harmony.api.routes.workspace import EscalationDraftView, EscalationView, ThreadWorkspace
from harmony.auth.views import (
    InternalModelView,
    MemberDirectory,
    MemberView,
    OrgRef,
    SessionView,
    WorldView,
)
from harmony.cli import app as cli
from harmony.cli_run import RunOptions, load_story
from harmony.db.models.choices import (
    EscalationKind,
    Fit,
    ModelRole,
    ThreadMessageKind,
    ThreadState,
)
from harmony.domain.conversations import SendMode
from harmony.domain.requests import RecipientStatus, RequestStatusView, WaitingOn
from harmony.errors import WorldRetired
from harmony.llm.errors import BadRequest
from harmony.llm.pricing import HAIKU_5_5, OPUS_5_5, call_cost, worst_case_cost
from harmony.llm.requests import ROLE_PROFILES, Tier, production_body
from harmony.llm.scripted import ToolUseBlock, load_cassettes
from harmony.seeds.loader import load_scenario
from harmony.seeds.schema import InlineQuestion, Resolved, Routed, StorySpec
from harmony.storyrun.api import ApiError, HarmonyApi
from harmony.storyrun.beats import free_form, recast
from harmony.storyrun.checks import (
    EmptyEndTurn,
    SentRequest,
    consults_after_send,
    duplicate_auto_recipients,
    empty_end_turn_nudged,
    ended_at,
    opener_leak,
    silent_turn_pairs,
    status_checks_after_send,
)
from harmony.storyrun.decide import (
    NO_DRAFT_TEXT,
    ApproveAll,
    PendingEscalation,
    PromptDecisions,
    StoryDecisions,
)
from harmony.storyrun.play import StoryPlayer
from harmony.storyrun.report import (
    beat_lines,
    chat_lines,
    failure_lines,
    latency_lines,
    model_lines,
    thread_lines,
)
from harmony.storyrun.spend import (
    CallRow,
    TurnCalls,
    as_measured,
    at_production_prices,
    peak_usd,
    percentile_ms,
)
from harmony.storyrun.worker import MODEL_ATTEMPTS, ActivityLog, active_pollers, exited
from harmony.temporal.policies import CHAT_TASK_QUEUE, MAIN_TASK_QUEUE, MODEL_TASK_QUEUE
from harmony.temporal.workflows.payloads import (
    BUDGET_EXHAUSTED_ERROR,
    CardResolved,
    EscalationResolved,
    conversation_workflow_id,
    thread_workflow_id,
)

SCENARIO = load_scenario("harmony-demo")
STORY = cast(StorySpec, SCENARIO.story)
NOW = datetime(2026, 10, 26, 9, tzinfo=UTC)
WORLD = uuid4()
BRIEF = "XRD and SEM/EDS on 12 powders by Nov 6, and a holder\n\nWhat is needed, in prose."
CARD_FITS: dict[str, Fit] = {"northlake": "strong", "ridgeline": "strong", "calder": "no"}
CASSETTES = Path(__file__).parents[1] / "fixtures" / "cassettes"


def org_ref(slug: str) -> OrgRef:
    spec = next(o for o in SCENARIO.orgs if o.slug == slug)
    return OrgRef(id=UUID(int=hash(slug) % 2**64), slug=slug, name=spec.name)


def member_view(slug: str) -> MemberView:
    spec, member = next((o, m) for o in SCENARIO.orgs for m in o.members if m.slug == slug)
    return MemberView(
        id=UUID(int=hash(("member", slug)) % 2**64),
        slug=slug,
        name=member.name,
        title=member.title,
        role=member.role,
        org=org_ref(spec.slug),
    )


def member_ref(slug: str) -> MemberRef:
    view = member_view(slug)
    return MemberRef(id=view.id, slug=view.slug, name=view.name)


@dataclass
class Escalation:
    id: UUID
    # The deciding org, and whose thread it is on.
    org: str
    counterparty: str
    member: str
    thread_id: UUID
    question: str
    reason: str
    go_ahead: bool = False
    kind: EscalationKind = "consult"
    resolution: ResolveEscalation | None = None

    @property
    def thread_org(self) -> str:
        return self.counterparty if self.org == "henderson" else self.org


@dataclass
class FakeStory:
    """A world that plays the suggested story as the backend would, a poll at a time.

    Once the outreach is approved, Ridgeline's seal question reaches Alex in the chat
    and Northlake's slot reaches its assignee's inbox. Once an answer is released, that
    thread's go-ahead reaches Alex; once he says yes, the thread closes. Once every
    thread has closed the request closes, and on the next poll Claude sums up.

    As in the real contract, a request's `waiting_on` names only Henderson Lab's own
    assignees, and an inbox's `needs_you` only the acting member's escalations: the
    player finds Northlake's escalation by reading Northlake's members' inboxes.
    """

    acting: str = "alex"
    conversation_id: UUID = field(default_factory=uuid4)
    card: UUID = field(default_factory=uuid4)
    request_id: UUID = field(default_factory=uuid4)
    threads: dict[str, UUID] = field(
        default_factory=lambda: {"northlake": uuid4(), "ridgeline": uuid4()}
    )
    items: list[ChatItem] = field(default_factory=list[ChatItem])
    card_status: str = "pending"
    approved: ApproveCard | None = None
    escalations: dict[UUID, Escalation] = field(default_factory=dict[UUID, Escalation])
    # Thread org → how its thread ended.
    ended: dict[str, ThreadState] = field(default_factory=dict[str, ThreadState])
    followed: set[UUID] = field(default_factory=set[UUID])
    calls: list[str] = field(default_factory=list[str])
    polls: int = 0
    # Claude never proposes anything: the run can only time out.
    stalled: bool = False
    # Claude never sums up once the request closes.
    silent: bool = False
    # Who Northlake's consult is routed to.
    northlake_assignee: str = "helen"
    # Northlake's thread expires once the outreach is approved.
    northlake_expires: bool = False
    # How long the world has been live when the story starts.
    world_age: timedelta = timedelta(days=4)
    # The chat's send-mode override, as the player started it.
    send_without_confirmation: bool | None = None

    # --- HarmonyApi ---------------------------------------------------------------

    async def start_session(self, scenario: str) -> SessionView:
        assert scenario == "harmony-demo"
        return self._session()

    async def act_as(self, member_id: UUID) -> SessionView:
        self.acting = next(m.slug for m in self._members() if m.id == member_id)
        self.calls.append(f"act-as {self.acting}")
        return self._session()

    async def members(self) -> MemberDirectory:
        return MemberDirectory(featured=self._members(), all=[])

    async def start_conversation(
        self, text: str, *, send_without_confirmation: bool | None = None
    ) -> ConversationCreated:
        self.send_without_confirmation = send_without_confirmation
        self.items.append(UserTextItem(id="msg:1", at=NOW, text=text, member_id=None))
        return ConversationCreated(id=self.conversation_id, message_id=uuid4())

    async def conversation(self, conversation_id: UUID) -> ConversationDetail:
        assert self.acting == "alex"
        self._advance()
        return ConversationDetail(
            id=conversation_id,
            title="Characterization",
            owner=member_ref("alex"),
            items=self.items if self.stalled else [*self.items, *self.card_items()],
            pending_card_id=self.card if self.card_status == "pending" else None,
            queued_message_ids=[],
            turn_in_flight=False,
            looking=False,
            turn_failure=None,
            send_mode=SendMode(override=None, org_default=False, effective=False),
            version=self.polls,
        )

    async def approve_card(self, card_id: UUID, body: ApproveCard) -> CardResolved:
        assert card_id == self.card
        self.card_status, self.approved = "approved", body
        self.calls.append("approve outreach to " + ", ".join(self._recipients()))
        return CardResolved(card_id=card_id, status="approved", request_id=self.request_id)

    async def reject_card(self, card_id: UUID, feedback: str) -> CardResolved:
        raise AssertionError("the story approves")

    async def request(self, request_id: UUID) -> RequestStatusView:
        recipients = [self._recipient(slug) for slug in self._recipients()]
        return RequestStatusView(
            id=request_id,
            title="XRD and SEM/EDS on 12 powders by Nov 6, and a holder",
            status="closed" if self._request_closed() else "active",
            brief=BRIEF,
            conversation_id=self.conversation_id,
            created_at=NOW,
            screened=None,
            recipients=recipients,
            version=self.polls,
        )

    async def inbox(self) -> Inbox:
        needs_you = [
            InboxItem(
                thread_id=e.thread_id,
                title="XRD and SEM/EDS on 12 powders by Nov 6, and a holder",
                conversation_id=None,
                side="responder",
                counterparty=org_ref(e.counterparty),
                thread_state="awaiting_principal",
                side_to_move="responder",
                escalation=InboxEscalation(
                    id=e.id,
                    kind=e.kind,
                    status="open",
                    question=e.question,
                    reason=e.reason,
                    assignees=[member_ref(e.member)],
                    created_at=NOW,
                    version=1,
                ),
                shared_by=None,
                shared_question=None,
                access_reasons=["routed"],
                last_activity_at=NOW,
            )
            for e in self.escalations.values()
            if e.member == self.acting and e.resolution is None
        ]
        return Inbox(
            needs_you=needs_you,
            with_your_team=[],
            claude_handling=[],
            shared_with_you=[],
            done=[],
        )

    async def workspace(self, thread_id: UUID) -> ThreadWorkspace:
        escalations = [
            EscalationView(
                id=e.id,
                kind=e.kind,
                status="open" if e.resolution is None else "resolved",
                consult_id=uuid4(),
                reason=e.reason,
                assignees=[member_ref(e.member)],
                current_draft_rev=1,
                drafts=[
                    EscalationDraftView(
                        draft_rev=1,
                        answer="As drafted.",
                        route=None,
                        rationale="From the records.",
                        activity=[],
                        rewritten_by=None,
                        created_at=NOW,
                    )
                ],
                resolution=None,
                resolved_by=None,
                resolved_at=None,
                created_at=NOW,
                version=1,
            )
            for e in self.escalations.values()
            if e.thread_id == thread_id
        ]
        return ThreadWorkspace(
            thread_id=thread_id,
            side="requester",
            consults=[],
            escalations=escalations,
            notes=[],
            representative=None,
            close_summary=None,
            steers=[],
            guidance=None,
            earlier_summaries=[],
            version=1,
        )

    async def resolve(self, escalation_id: UUID, body: ResolveEscalation) -> EscalationResolved:
        escalation = self.escalations[escalation_id]
        assert self.acting == escalation.member
        escalation.resolution = body
        self.calls.append(f"resolve {escalation.org} {escalation.kind} as {self.acting}")
        return EscalationResolved(escalation_id=escalation_id, action=body.action)

    async def thread(self, thread_id: UUID) -> ThreadDetail:
        raise ApiError("GET", f"/threads/{thread_id}", 404, "this fake serves no transcripts")

    # --- The world ------------------------------------------------------------------

    def _advance(self) -> None:
        self.polls += 1
        if self.stalled or self.card_status != "approved":
            return
        if self._request_closed():
            if not self.silent and not self._summarized():
                self.items.append(
                    AssistantTextItem(
                        id="msg:2",
                        at=NOW + timedelta(minutes=10),
                        markdown="$1,735 in total, inside your budget.",
                        turn_id=None,
                    )
                )
            return
        if not self.escalations:
            self._open("henderson", "ridgeline", "alex", "Which seal material?")
            if self.northlake_expires:
                self.ended["northlake"] = "expired"
            else:
                self._open("northlake", "henderson", self.northlake_assignee, "XRD by Nov 6?")
            return
        for e in list(self.escalations.values()):
            if e.resolution is None or e.id in self.followed:
                continue
            self.followed.add(e.id)
            if e.go_ahead:
                self.ended[e.thread_org] = "closed"
            else:
                question = f"Go ahead with {e.thread_org}?"
                self._open("henderson", e.thread_org, "alex", question, go_ahead=True)

    def _open(
        self, org: str, counterparty: str, member: str, question: str, *, go_ahead: bool = False
    ) -> None:
        thread_org = counterparty if org == "henderson" else org
        escalation = Escalation(
            uuid4(),
            org,
            counterparty,
            member,
            self.threads[thread_org],
            question,
            reason="Only a person can say.",
            go_ahead=go_ahead,
        )
        self.escalations[escalation.id] = escalation

    def _recipients(self) -> list[str]:
        chosen = self.approved.recipients if self.approved is not None else None
        return [slug for slug in CARD_FITS if chosen is None or org_ref(slug).id in chosen]

    def _request_closed(self) -> bool:
        return self.card_status == "approved" and all(
            slug in self.ended for slug in self._recipients()
        )

    def _summarized(self) -> bool:
        return any(isinstance(item, AssistantTextItem) for item in self.items)

    def card_items(self) -> list[ChatItem]:
        card = OutreachCardItem(
            id=f"card:{self.card}",
            at=NOW,
            card_id=self.card,
            status="approved" if self.card_status == "approved" else "pending",
            brief=BRIEF,
            recipients=[
                CardRecipient(org=org_ref(slug), fit=fit, reason="Fits.")
                for slug, fit in CARD_FITS.items()
            ],
            screened=None,
            feedback=None,
            rejected_with=None,
            request_id=self.request_id if self.card_status == "approved" else None,
            version=1,
        )
        questions: list[ChatItem] = [
            InlineQuestionItem(
                id=f"esc:{e.id}",
                at=NOW,
                escalation_id=e.id,
                thread_id=e.thread_id,
                question=e.question,
                from_org=org_ref(e.counterparty),
                status="open" if e.resolution is None else "resolved",
                action=e.resolution.action if e.resolution else None,
                answer=e.resolution.text if e.resolution else None,
                answered_by=None,
                answered_at=None,
            )
            for e in self.escalations.values()
            if e.org == "henderson"
        ]
        return [card, *questions]

    def _recipient(self, slug: str) -> RecipientStatus:
        """As `domain.requests.status` builds it: an ended thread first, then an open
        escalation on either side; `waiting_on` names Henderson Lab's assignees only. A
        recipient without a thread (Calder, here) is only screened."""
        thread = self.threads.get(slug)
        if thread is None:
            return RecipientStatus(
                org=org_ref(slug),
                fit=CARD_FITS[slug],
                reason="Fits.",
                sent=slug in self._recipients(),
                participant="screened",
                thread_id=None,
                thread_state=None,
                waiting_on=None,
                latest_line=None,
                close_summary=None,
                last_activity_at=None,
            )
        open_here = [
            e for e in self.escalations.values() if e.thread_id == thread and e.resolution is None
        ]
        ours = next((e for e in open_here if e.org == "henderson"), None)
        state = self.ended.get(slug, "active")
        participant = "closed" if slug in self.ended else "needs_human" if ours else "replied"
        return RecipientStatus(
            org=org_ref(slug),
            fit=CARD_FITS[slug],
            reason="Fits.",
            sent=True,
            participant=participant,
            thread_id=thread,
            thread_state=state,
            waiting_on=WaitingOn(member=member_ref(ours.member), since=NOW) if ours else None,
            latest_line="Thanks.",
            close_summary="Booked." if state == "closed" else None,
            last_activity_at=NOW,
        )

    def _members(self) -> list[MemberView]:
        return [member_view(slug) for slug in ("alex", "maya", "helen", "raj", "dana")]

    def _session(self) -> SessionView:
        return SessionView(
            session_id=UUID(int=1),
            epoch=1,
            lens="desk",
            world_id=WORLD,
            world=WorldView(
                id=WORLD,
                scenario="harmony-demo",
                mode="live",
                status="live",
                created_at=datetime.now(UTC) - self.world_age,
                ff_offset_s=0,
                skip_approvals=False,
            ),
            member=member_view(self.acting),
            internal_model=InternalModelView(
                id="claude-opus-5-5", label="Scripted", adaptive=False
            ),
        )


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def player(world: FakeStory, **kwargs: Any) -> tuple[StoryPlayer, list[str]]:
    clock, lines = FakeClock(), list[str]()
    return (
        StoryPlayer(
            cast(HarmonyApi, world),
            SCENARIO,
            STORY,
            StoryDecisions(STORY),
            log=lines.append,
            clock=clock,
            sleep=clock.sleep,
            **kwargs,
        ),
        lines,
    )


# --- The play loop --------------------------------------------------------------------


def actions(world: FakeStory) -> list[str]:
    return [call for call in world.calls if not call.startswith("act-as")]


def answers(world: FakeStory) -> list[tuple[str, str, str | None]]:
    """(deciding org, thread org, text) of each resolution, in the order opened."""
    return [
        (e.org, e.thread_org, e.resolution.text)
        for e in world.escalations.values()
        if e.resolution is not None
    ]


async def test_the_story_plays_through_every_beat() -> None:
    world = FakeStory()
    play, lines = player(world)
    run = await play.play()
    assert (run.outcome, run.detail) == ("done", None)
    assert run.missing() == []
    assert actions(world) == [
        "approve outreach to northlake, ridgeline",
        "resolve henderson consult as alex",
        "resolve northlake consult as helen",
        "resolve henderson consult as alex",
        "resolve henderson consult as alex",
    ]
    # Nothing in the request's status named Helen: she was read as a Northlake member,
    # first because the story's rule names her.
    northlake_reads = [c for c in world.calls if c in ("act-as helen", "act-as raj")]
    assert northlake_reads[0] == "act-as helen"
    assert answers(world) == [
        (
            "henderson",
            "ridgeline",
            "Kalrez, please. The powders may sit under argon for a few weeks.",
        ),
        ("northlake", "northlake", None),
        ("henderson", "ridgeline", "Yes, go ahead with Ridgeline."),
        ("henderson", "northlake", "Yes, go ahead with Northlake."),
    ]
    assert all(
        e.resolution is not None and (e.resolution.action, e.resolution.draft_rev) == ("approve", 1)
        for e in world.escalations.values()
    )
    # Counted from the anchor, not from the world's date four days on.
    assert any("by Nov 6," in line for line in lines)
    assert any("Kalrez, please." in line for line in lines)
    assert all(line.startswith("  ok") for line in beat_lines(run))


async def test_the_outreach_leaves_out_the_near_miss() -> None:
    world = FakeStory()
    play, _ = player(world)
    await play.play()
    assert world.approved is not None
    assert world.approved.recipients == [org_ref("northlake").id, org_ref("ridgeline").id]


async def test_settling_waits_for_the_request_to_close_and_claudes_summary() -> None:
    world = FakeStory()
    play, _ = player(world)
    run = await play.play()
    assert run.outcome == "done"
    assert await play.settle(run)
    chat, _ = await play.transcripts(run)
    assert chat is not None
    assert "$1,735 in total" in "\n".join(chat_lines(chat))


async def test_settling_gives_up_when_claude_stays_silent() -> None:
    play, _ = player(FakeStory(silent=True), settle_s=5)
    run = await play.play()
    assert run.outcome == "done"
    assert not await play.settle(run)


async def test_an_escalation_routed_to_someone_no_rule_names_is_still_resolved() -> None:
    world = FakeStory(northlake_assignee="raj")
    play, _ = player(world, timeout_s=60)
    run = await play.play()
    assert "resolve northlake consult as raj" in world.calls
    # The rule for Northlake applies to whoever decides: approve as drafted.
    assert ("northlake", "northlake", None) in answers(world)
    # The story's own beats name Helen, so they stay missing, but the run doesn't
    # wait out its timeout: the request closes, and the run says so.
    assert run.outcome == "ended"
    assert {step.id for step in run.missing()} == {"northlake-routed", "northlake-approved"}


async def test_a_conversation_that_expires_ends_the_run_early() -> None:
    world = FakeStory(northlake_expires=True)
    play, _ = player(world, timeout_s=600)
    run = await play.play()
    assert run.outcome == "ended"
    assert run.detail is not None
    assert "conversation with Northlake Characterization Core expired" in run.detail


async def test_a_model_call_that_failed_for_good_ends_the_run() -> None:
    world = FakeStory()
    # The worker's map is empty when the player starts and fills as activities fail.
    failures: dict[str, str] = {}
    play, _ = player(world, failures=failures)
    failures[thread_workflow_id(WORLD, world.threads["northlake"])] = "BadRequest: no entry"
    run = await play.play()
    assert run.outcome == "failed"
    assert run.detail == "a model call failed for good: BadRequest: no entry"


async def test_another_workflows_failure_does_not_stop_the_story() -> None:
    elsewhere = {conversation_workflow_id(uuid4(), uuid4()): "BadRequest: no entry"}
    play, _ = player(FakeStory(), failures=elsewhere)
    run = await play.play()
    assert run.outcome == "done"


async def test_the_run_stops_at_the_timeout_and_names_what_is_missing() -> None:
    play, _ = player(FakeStory(stalled=True), timeout_s=5)
    run = await play.play()
    assert run.outcome == "timeout"
    assert next(step.id for step in run.missing()) == "approve-outreach"
    assert any(line.startswith("  MISSING") for line in beat_lines(run))


async def test_transcripts_skip_the_threads_they_cannot_read() -> None:
    world = FakeStory()
    play, lines = player(world)
    run = await play.play()
    chat, threads = await play.transcripts(run)
    assert chat is not None
    assert threads == []
    assert any("this fake serves no transcripts" in line for line in lines)
    text = "\n".join(chat_lines(chat))
    assert "[outreach card, approved] XRD and SEM/EDS on 12 powders by Nov 6, and a holder" in text
    assert "to: Northlake Characterization Core, Ridgeline Fabrication" in text


async def test_a_stopped_worker_fails_the_run() -> None:
    worker: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    worker.set_result(None)
    play, _ = player(FakeStory(), worker=worker)
    run = await play.play()
    assert (run.outcome, run.detail) == ("failed", "the worker stopped")


@dataclass
class FakeChat(FakeStory):
    """A free-form chat whose one message yields two requests, to Northlake and then to
    Ridgeline, each thread closing three polls after its request starts. Claude raises
    the second card once the first went out, and sums up once both requests closed.
    With `auto` the chat sends without review: each card goes out as it is raised;
    otherwise each waits for the player's approval."""

    auto: bool = False
    # Card → its request, None while it waits.
    raised: dict[UUID, UUID | None] = field(default_factory=dict[UUID, UUID | None])
    # Request → its org and the poll it started at.
    started: dict[UUID, tuple[str, int]] = field(default_factory=dict[UUID, tuple[str, int]])
    approvals: list[ApproveCard] = field(default_factory=list[ApproveCard])
    seen_closed: set[UUID] = field(default_factory=set[UUID])

    async def conversation(self, conversation_id: UUID) -> ConversationDetail:
        assert self.acting == "alex"
        self.polls += 1
        if len(self.raised) < 2 and None not in self.raised.values():
            card_id = uuid4()
            self.raised[card_id] = None
            if self.auto:
                self._start(card_id)
        elif len(self.started) == 2 and all(map(self._closed, self.started)):
            if not self._summarized():
                self.items.append(
                    AssistantTextItem(
                        id="msg:2",
                        at=NOW + timedelta(minutes=10),
                        markdown="Both booked.",
                        turn_id=None,
                    )
                )
        pending = next((c for c, r in self.raised.items() if r is None), None)
        return ConversationDetail(
            id=conversation_id,
            title="Characterization",
            owner=member_ref("alex"),
            items=[*self.items, *(self._card(c) for c in self.raised)],
            pending_card_id=pending,
            queued_message_ids=[],
            turn_in_flight=len(self.raised) < 2 and pending is None,
            looking=False,
            turn_failure=None,
            send_mode=SendMode(
                override=self.send_without_confirmation, org_default=False, effective=self.auto
            ),
            version=self.polls,
        )

    async def approve_card(self, card_id: UUID, body: ApproveCard) -> CardResolved:
        assert not self.auto, "nobody approves a card sent without review"
        self.approvals.append(body)
        request_id = self._start(card_id)
        return CardResolved(card_id=card_id, status="approved", request_id=request_id)

    async def request(self, request_id: UUID) -> RequestStatusView:
        org, _ = self.started[request_id]
        closed = self._closed(request_id)
        if closed:
            self.seen_closed.add(request_id)
        return RequestStatusView(
            id=request_id,
            title=f"Work with {org}",
            status="closed" if closed else "active",
            brief=BRIEF,
            conversation_id=self.conversation_id,
            created_at=NOW,
            screened=None,
            recipients=[
                RecipientStatus(
                    org=org_ref(org),
                    fit="strong",
                    reason="Fits.",
                    sent=True,
                    participant="closed" if closed else "replied",
                    thread_id=self.threads[org],
                    thread_state="closed" if closed else "active",
                    waiting_on=None,
                    latest_line="Booked." if closed else "Checking.",
                    close_summary="Booked." if closed else None,
                    last_activity_at=NOW,
                )
            ],
            version=self.polls,
        )

    def _start(self, card_id: UUID) -> UUID:
        request_id = uuid4()
        self.raised[card_id] = request_id
        self.started[request_id] = (self._org(card_id), self.polls)
        return request_id

    def _closed(self, request_id: UUID) -> bool:
        return self.polls - self.started[request_id][1] >= 3

    def _org(self, card_id: UUID) -> str:
        return ("northlake", "ridgeline")[list(self.raised).index(card_id)]

    def _card(self, card_id: UUID) -> OutreachCardItem:
        request_id = self.raised[card_id]
        return OutreachCardItem(
            id=f"card:{card_id}",
            at=NOW,
            card_id=card_id,
            status="pending" if request_id is None else "approved",
            auto=self.auto,
            brief=BRIEF,
            recipients=[
                CardRecipient(org=org_ref(self._org(card_id)), fit="strong", reason="Fits.")
            ],
            screened=None,
            feedback=None,
            rejected_with=None,
            request_id=request_id,
            version=1,
        )


@pytest.mark.parametrize("auto", [False, True])
async def test_one_chat_yields_two_requests_and_both_are_followed_to_the_end(
    auto: bool,
) -> None:
    world = FakeChat(auto=auto)
    clock = FakeClock()
    story = free_form("alex", "Book XRD on {+11d} and a holder.")
    play = StoryPlayer(
        cast(HarmonyApi, world),
        SCENARIO,
        story,
        ApproveAll(),
        log=list[str]().append,
        clock=clock,
        sleep=clock.sleep,
        send_without_review=auto,
    )
    run = await play.play()
    assert (run.outcome, run.detail) == ("done", None)
    assert run.request_ids == list(world.started)
    assert len(run.request_ids) == 2
    assert world.seen_closed == set(world.started), "each request polled until it closed"
    assert run.threads == {
        world.threads["northlake"]: "northlake",
        world.threads["ridgeline"]: "ridgeline",
    }
    assert world.send_without_confirmation is (True if auto else None)
    if auto:
        assert world.approvals == []
    else:
        assert world.approvals == [
            ApproveCard(recipients=[org_ref("northlake").id]),
            ApproveCard(recipients=[org_ref("ridgeline").id]),
        ]
    assert await play.settle(run)


# --- Decisions ------------------------------------------------------------------------


def pending(org: str, counterparty: str, *, with_draft: bool = True) -> PendingEscalation:
    return PendingEscalation(
        escalation_id=uuid4(),
        kind="consult",
        org=org,
        member=member_ref("helen" if org == "northlake" else "alex"),
        counterparty=counterparty,
        thread_id=uuid4(),
        question="?",
        reason=None,
        draft="As drafted." if with_draft else None,
        draft_rev=2 if with_draft else None,
    )


async def test_decisions_use_the_story_rules_in_order() -> None:
    decide = StoryDecisions(STORY)
    seal = await decide.escalation(pending("henderson", "ridgeline"))
    assert (seal.action, seal.draft_rev) == ("approve", 2)
    assert seal.text is not None
    assert seal.text.startswith("Kalrez")
    slot = await decide.escalation(pending("northlake", "henderson"))
    assert (slot.action, slot.text) == ("approve", None), "as drafted"
    northlake = await decide.escalation(pending("henderson", "northlake"))
    assert northlake.text == "Yes, go ahead with Northlake."
    ridgeline = await decide.escalation(pending("henderson", "ridgeline"))
    assert ridgeline.text == "Yes, go ahead with Ridgeline."
    # No rule left: the draft goes out as written, or with no draft, a decline.
    again = await decide.escalation(pending("henderson", "ridgeline"))
    assert (again.action, again.text, again.draft_rev) == ("approve", None, 2)
    nothing = await decide.escalation(pending("calder", "henderson", with_draft=False))
    assert (nothing.action, nothing.text) == ("decline", NO_DRAFT_TEXT)


async def test_an_escalation_asked_again_keeps_its_rule() -> None:
    decide = StoryDecisions(STORY)
    seal = pending("henderson", "ridgeline")
    first, second = await decide.escalation(seal), await decide.escalation(seal)
    assert first.text == second.text
    assert first.text is not None
    assert first.text.startswith("Kalrez")
    go_ahead = await decide.escalation(pending("henderson", "ridgeline"))
    assert go_ahead.text == "Yes, go ahead with Ridgeline."


def card(
    fits: dict[str, Fit], *, suggested: frozenset[str] | set[str] = frozenset()
) -> OutreachCardItem:
    return OutreachCardItem(
        id="card:1",
        at=NOW,
        card_id=uuid4(),
        status="pending",
        brief=BRIEF,
        recipients=[
            CardRecipient(
                org=org_ref(slug), fit=fit, reason="Fits.", suggested=slug in suggested or None
            )
            for slug, fit in fits.items()
        ],
        screened=None,
        feedback=None,
        rejected_with=None,
        request_id=None,
        version=1,
    )


async def test_the_outreach_goes_to_the_orgs_the_story_expects() -> None:
    decide = StoryDecisions(STORY)
    expected = [org_ref("northlake").id, org_ref("ridgeline").id]
    assert await decide.outreach(card(CARD_FITS)) == ApproveCard(recipients=expected)
    # The card's fits don't decide: a recipient the story needs stays, whatever its fit.
    unfit: dict[str, Fit] = {"northlake": "no", "ridgeline": "strong", "calder": "no"}
    assert await decide.outreach(card(unfit)) == ApproveCard(recipients=expected)
    assert await decide.outreach(card({"northlake": "strong", "ridgeline": "possible"})) == (
        ApproveCard(recipients=expected)
    )
    # None the story expects: the rows the card proposes, named, never none.
    calder = [org_ref("calder").id]
    assert await decide.outreach(card({"calder": "no"})) == ApproveCard(recipients=calder)
    calder_suggested = card({"ridgeline": "strong", "calder": "possible"}, suggested={"calder"})
    assert await decide.outreach(calder_suggested) == ApproveCard(
        recipients=[org_ref("ridgeline").id]
    ), "the story's org is kept, not the card's suggestion"
    assert await decide.outreach(
        card({"calder": "possible", "vantage": "no"}, suggested={"vantage"})
    ) == ApproveCard(recipients=[org_ref("vantage").id]), "the card's suggestion"
    assert await decide.outreach(card({"calder": "possible", "vantage": "no"})) == ApproveCard(
        recipients=calder
    ), "without suggestions, the rows that fit"


async def test_approving_all_names_every_listed_row_and_takes_drafts_as_written() -> None:
    decide = ApproveAll()
    every = [org_ref(slug).id for slug in CARD_FITS]
    assert await decide.outreach(card(CARD_FITS)) == ApproveCard(recipients=every)
    drafted = await decide.escalation(pending("northlake", "henderson"))
    assert (drafted.action, drafted.text, drafted.draft_rev) == ("approve", None, 2)
    nothing = await decide.escalation(pending("calder", "henderson", with_draft=False))
    assert (nothing.action, nothing.text) == ("decline", NO_DRAFT_TEXT)


async def test_prompting_defaults_to_the_storys_choice() -> None:
    shown: list[str] = []
    decide = PromptDecisions(STORY, ask=lambda prompt, default: default, show=shown.append)
    chosen = await decide.outreach(card(CARD_FITS))
    assert chosen == ApproveCard(recipients=[org_ref("northlake").id, org_ref("ridgeline").id])
    text = "\n".join(shown)
    assert "XRD and SEM/EDS on 12 powders by Nov 6, and a holder" in text
    assert "Calder Computational (no), left out" in text
    seal = pending("henderson", "ridgeline")
    resolved = await decide.escalation(seal)
    assert resolved.action == "approve"
    assert resolved.text is not None
    assert resolved.text.startswith("Kalrez")


def test_recasting_hands_the_story_members_part_to_another() -> None:
    story = recast(STORY, "maya")
    assert story.member == "maya"
    beats = {step.id: step.expect for step in story.steps}
    assert beats["seal-question"] == InlineQuestion(
        beat="inline_question", org="ridgeline", member="maya"
    )
    assert beats["seal-answer"] == Resolved(
        beat="resolved", org="henderson", member="maya", action="approve"
    )
    assert beats["northlake-routed"] == Routed(beat="routed", org="northlake", member="helen")
    assert beats["go-ahead"] == InlineQuestion(
        beat="inline_question", org="northlake", member="maya"
    )
    assert {step.id: step.act_as for step in story.steps}["go-ahead"] == "maya"
    assert [(rule.org, rule.member) for rule in story.humans] == [
        ("henderson", "maya"),
        ("northlake", "helen"),
        ("henderson", "maya"),
        ("henderson", "maya"),
    ]
    assert recast(STORY, STORY.member) is STORY


# --- The report -----------------------------------------------------------------------


def test_a_close_shows_as_a_marker_without_its_summary() -> None:
    henderson, northlake = org_ref("henderson"), org_ref("northlake")

    def line(seq: int, kind: ThreadMessageKind, body: str) -> ThreadMessageView:
        return ThreadMessageView(
            id=uuid4(),
            seq=seq,
            from_org_id=northlake.id,
            kind=kind,
            body=body,
            provenance="agent",
            created_at=NOW,
        )

    thread = ThreadDetail(
        id=uuid4(),
        title="XRD",
        state="closed",
        side="requester",
        requester=henderson,
        responder=northlake,
        access_reason="routed",
        access_reasons=["routed"],
        last_activity_at=NOW,
        run=1,
        end_reason="closed",
        ended_at=NOW,
        closed_by="responder",
        run_finished_at=NOW,
        version=1,
        side_to_move=None,
        deadline_at=None,
        created_at=NOW,
        messages=[line(1, "message", "Booked: $1,410."), line(2, "close", "")],
        access=[],
        reopen=None,
        runs=[],
    )
    text = "\n".join(thread_lines(thread))
    assert "Booked: $1,410." in text
    assert "2. Northlake Characterization Core closed the conversation (close)." in text


def test_models_other_than_the_override_are_a_setup_failure() -> None:
    lines, failed_setup = model_lines([HAIKU_5_5], HAIKU_5_5)
    assert (lines, failed_setup) == ([f"  models used: {HAIKU_5_5}"], False)
    lines, failed_setup = model_lines([HAIKU_5_5, OPUS_5_5], HAIKU_5_5)
    assert failed_setup
    assert f"SETUP FAILURE: {OPUS_5_5} isn't the override {HAIKU_5_5}" in lines[-1]
    assert model_lines([OPUS_5_5], None) == ([f"  models used: {OPUS_5_5}"], False)


def test_latency_is_reported_at_the_nearest_rank() -> None:
    assert percentile_ms([400, 100, 300, 200], 50) == 200
    assert percentile_ms(list(range(1, 101)), 95) == 95
    assert percentile_ms([], 50) is None
    assert latency_lines([1000, 3000]) == ["  turn latency: p50 1.0s, p95 3.0s (of 2 turns)"]


# --- Re-pricing (3.14) ----------------------------------------------------------------

BODY: dict[str, JsonValue] = {
    "model": HAIKU_5_5,
    "max_tokens": 4000,
    "messages": [{"role": "user", "content": "Find XRD time for 12 powders."}],
}
USAGE: dict[str, JsonValue] = {"input_tokens": 1200, "output_tokens": 600}


def timed(role: ModelRole, tier: Tier, start_s: int, end_s: int) -> TurnCalls:
    """A turn of one call, as it ran on the override's Haiku."""
    call = CallRow(json.dumps(BODY), USAGE, Decimal("0.0030"), NOW + timedelta(seconds=start_s))
    return TurnCalls(
        role, tier, NOW + timedelta(seconds=start_s), NOW + timedelta(seconds=end_s), (call,)
    )


def production(role: ModelRole, tier: Tier) -> tuple[Decimal, Decimal]:
    """The turn's hold and settled cost on its role's production model."""
    model = ROLE_PROFILES[(role, tier)].model
    hold = worst_case_cost(production_body(BODY, role, tier))
    return hold, call_cost(USAGE, requested_model=model, served_model=model)


def test_the_re_priced_peak_puts_each_turn_on_its_production_model() -> None:
    assert ROLE_PROFILES[("internal", "background")].model == HAIKU_5_5
    assert ROLE_PROFILES[("representative", "featured")].model == OPUS_5_5
    chat_hold, chat_settled = production("internal", "background")
    rep_hold, rep_settled = production("representative", "featured")
    assert chat_hold == worst_case_cost(BODY), "background chat runs on Haiku in production"
    assert rep_hold > chat_hold
    assert rep_settled > chat_settled
    chat = timed("internal", "background", 0, 10)
    # Overlapping: both turns hold their reservations at once.
    rep = timed("representative", "featured", 5, 20)
    assert peak_usd([chat, rep], at_production_prices) == chat_hold + rep_hold
    # Back to back: the chat turn has settled when the representative's reserves.
    later = timed("representative", "featured", 10, 20)
    assert peak_usd([chat, later], at_production_prices) == chat_settled + rep_hold
    # As measured, both ran on Haiku.
    assert peak_usd([chat, rep], as_measured) == 2 * worst_case_cost(BODY)


# --- Engine checks --------------------------------------------------------------------


def openers() -> dict[str, str]:
    """Henderson's first message to each recipient in the story's cassette."""
    entries = load_cassettes(CASSETTES / "story_suggested_threads.yaml")
    found: dict[str, str] = {}
    for (_, org, role, thread, turn, step), entry in entries.items():
        if (org, role, turn, step) != ("henderson", "representative", 1, 0) or thread is None:
            continue
        for block in entry.content:
            if isinstance(block, ToolUseBlock) and block.name == "send_message":
                found[thread.removeprefix("henderson.")] = str(block.input["text"])
    return found


def test_the_opener_patterns_pass_the_cassette_openers() -> None:
    said = openers()
    # Henderson Lab's own affiliation and the XRD dome mount are its own business.
    assert "Northlake University" in said["ridgeline"]
    assert "dome mount" in said["northlake"]
    assert opener_leak("harmony-demo", "northlake", said["northlake"]) is None
    assert opener_leak("harmony-demo", "ridgeline", said["ridgeline"]) is None
    assert opener_leak("harmony-demo", "calder", said["calder"]) is None


@pytest.mark.parametrize(
    ("recipient", "opener", "leak"),
    [
        ("ridgeline", "We also need SEM/EDS on the powders.", "SEM"),
        (
            "ridgeline",
            "Northlake's characterization core will run the XRD.",
            "characterization core",
        ),
        ("ridgeline", "We need XRD on 12 powders and a holder.", "XRD on 12"),
        ("northlake", "Ridgeline is making the holder.", "Ridgeline"),
        ("northlake", "We're having a transfer holder machined.", "holder"),
    ],
)
def test_the_opener_patterns_catch_another_recipients_part(
    recipient: str, opener: str, leak: str
) -> None:
    assert opener_leak("harmony-demo", recipient, opener) == leak


_TURN = UUID(int=21)
_CARD_CALL = "toolu_card"
_NOTHING_NEW_TEXT = "No new message from Henderson Lab since your last turn."


def _empty(step_idx: int, *opening: dict[str, JsonValue], notices: bool = False) -> EmptyEndTurn:
    return EmptyEndTurn(turn_id=_TURN, step_idx=step_idx, opening=list(opening), notices=notices)


def _text(text: str) -> dict[str, JsonValue]:
    return {"type": "text", "text": text}


def _result(tool_use_id: str, content: str = "Sent.") -> dict[str, JsonValue]:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}


@pytest.mark.parametrize(
    ("call", "approved", "nudged"),
    [
        # A new turn's first call answers the member, the network or the counterparty.
        (_empty(0, _text("Find an XRD lab.")), None, True),
        (_empty(0, _result("toolu_owed"), _text("Can you do Nov 4?")), None, True),
        # ...unless it is a representative's code-written "nothing new".
        (_empty(0, _text(_NOTHING_NEW_TEXT)), None, False),
        (_empty(0, _text(_NOTHING_NEW_TEXT), notices=True), None, False),
        # ...or only its held messages declined without a reason (a bare-text reply's
        # decline answers no call, so it is text).
        (_empty(0, _result("toolu_send", HELD_DECLINED)), None, False),
        (
            _empty(0, _result("toolu_a", HELD_DECLINED), _result("toolu_b", HELD_DECLINED)),
            None,
            False,
        ),
        (_empty(0, _text(HELD_DECLINED), notices=True), None, False),
        # A decline with a reason, or with the counterparty's message, asks for one.
        (
            _empty(0, _result("toolu_send", f"{HELD_DECLINED} What you may say:\nNot yet.")),
            None,
            True,
        ),
        (_empty(0, _result("toolu_send", HELD_DECLINED), _text("Can you do Nov 4?")), None, True),
        # A turn resumed at step 3 on its own card: quiet once the request went out.
        (_empty(3, _result(_CARD_CALL, "approved; request R sent to Northlake")), True, False),
        (_empty(3, _result(_CARD_CALL, "rejected: ask Ridgeline")), False, True),
        (_empty(3, _result(_CARD_CALL, "approved; request R sent"), notices=True), True, True),
        # After the turn's own tool results.
        (_empty(3, _result("toolu_search", "Two labs fit.")), None, False),
        (_empty(1, _result("toolu_send")), None, False),
    ],
)
def test_an_empty_end_turn_is_nudged_only_on_a_runs_first_call_that_owed_an_answer(
    call: EmptyEndTurn, approved: bool | None, nudged: bool
) -> None:
    """3.13 E1b, as the runtime splits it: a resumed turn's first call comes at step > 0,
    so the step alone doesn't say which call was a run's first."""
    cards = {} if approved is None else {(_TURN, _CARD_CALL): approved}
    assert empty_end_turn_nudged(call, cards) is nudged


def _request(chat: int, minute: int, *recipients: UUID, auto: bool = True) -> SentRequest:
    return SentRequest(
        id=UUID(int=1000 + minute),
        conversation_id=UUID(int=chat),
        created_at=datetime(2026, 10, 26, 9, minute, tzinfo=UTC),
        auto=auto,
        recipients=list(recipients),
    )


def test_a_duplicate_auto_recipient_is_one_still_in_touch_from_the_same_chat() -> None:
    northlake, ridgeline = UUID(int=1), UUID(int=2)
    names = {northlake: "Northlake", ridgeline: "Ridgeline"}
    first = _request(7, 0, northlake)
    reviewed = _request(7, 1, ridgeline, auto=False)
    # Northlake's thread ended at 9:05: a follow-up then is no duplicate.
    follow_up = _request(7, 10, northlake)
    # Ridgeline's reviewed thread is still open, its thread not even started.
    overlap = _request(7, 12, ridgeline)
    elsewhere = _request(8, 13, ridgeline)
    ended = {(first.id, northlake): datetime(2026, 10, 26, 9, 5, tzinfo=UTC)}

    found = duplicate_auto_recipients(
        [elsewhere, overlap, follow_up, reviewed, first], ended, names
    )

    assert found == [
        f"Ridgeline: request {overlap.id} while request {reviewed.id} was still in touch"
    ]


def test_a_thread_ended_when_it_first_went_terminal_not_at_its_last_summary() -> None:
    thread, other = UUID(int=31), UUID(int=32)
    at = "2026-10-26T09:{:02d}:00+00:00".format
    snapshots: list[tuple[UUID, int, dict[str, JsonValue]]] = [
        (thread, 3, {"state": "closed", "last_activity_at": at(7)}),
        (thread, 1, {"state": "active", "last_activity_at": at(1)}),
        # The same version reaches both parties' scopes.
        (thread, 2, {"state": "closed", "last_activity_at": at(5)}),
        (thread, 2, {"state": "closed", "last_activity_at": at(5)}),
        (other, 1, {"state": "awaiting_principal", "last_activity_at": at(2)}),
    ]

    assert ended_at(snapshots) == {thread: datetime(2026, 10, 26, 9, 5, tzinfo=UTC)}


def test_silent_turn_pairs_count_as_the_thread_does() -> None:
    """Two quiet turns in a row on one thread are a pair; an acting turn resets the run.
    A refusal isn't quiet, and once a turn closes or is refused, the last turn after it
    only summarizes (3.13 E3)."""
    one, two = UUID(int=41), UUID(int=42)
    t = [UUID(int=100 + i) for i in range(12)]
    turns: list[tuple[UUID, UUID | None]] = [
        (t[0], one),  # sent
        (t[1], one),  # quiet
        (t[2], two),  # quiet, another thread
        (t[3], one),  # quiet: a pair on thread one
        (t[4], two),  # sent
        (t[5], two),  # quiet
        (t[6], two),  # refused: not quiet, and it ends the thread
        (t[7], two),  # the last turn: only its summary counts
        (t[8], one),  # quiet
        (t[9], one),  # closed
        (t[10], one),  # the last turn, quiet
    ]
    acted = {t[0], t[4], t[9]}
    ends = {t[6], t[9]}

    assert silent_turn_pairs(turns, acted, ends) == 1


def test_consults_after_send_count_the_refusals_and_any_close_after_one() -> None:
    """A turn sends, consults (refused), then closes anyway: one of each. A consult
    before the send was answered, and a close in a turn with no refusal isn't counted."""
    refused_turn, answered_turn, closing_turn = UUID(int=51), UUID(int=52), UUID(int=53)

    def calls(*names: str) -> str:
        return json.dumps(
            [
                {"type": "tool_use", "id": f"{name}-{index}", "name": name, "input": {}}
                for index, name in enumerate(names)
            ]
        )

    def results(*answers: tuple[str, str]) -> str:
        return json.dumps(
            [{"type": "tool_result", "tool_use_id": id_, "content": text} for id_, text in answers]
        )

    refusal = CONSULT_AFTER_SEND.format(counterparty="Northlake")
    rows: list[tuple[UUID | None, str, str]] = [
        (answered_turn, "assistant", calls("consult")),
        (answered_turn, "user", results(("consult-0", "Nov 4 is free."))),
        (answered_turn, "assistant", calls("send_message")),
        (answered_turn, "user", results(("send_message-0", "Sent."))),
        (refused_turn, "assistant", calls("send_message", "consult")),
        (refused_turn, "user", results(("send_message-0", "Sent."), ("consult-1", refusal))),
        (refused_turn, "assistant", calls("close")),
        (refused_turn, "user", results(("close-0", "Closed."))),
        (closing_turn, "assistant", calls("close")),
        (closing_turn, "user", results(("close-0", "Closed."))),
    ]

    assert consults_after_send(rows) == (1, 1)


def test_status_checks_after_send_count_the_checks_once_a_request_went_out() -> None:
    """Decision 7: after its card's request went out, a turn has nothing to check. A
    check before the card, or in a turn whose card was declined, isn't counted."""
    sent_turn, declined_turn = UUID(int=61), UUID(int=62)

    def blocks(*content: dict[str, str]) -> str:
        return json.dumps(list(content))

    def use(call_id: str, name: str) -> dict[str, str]:
        return {"type": "tool_use", "id": call_id, "name": name}

    def result(call_id: str) -> dict[str, str]:
        return {"type": "tool_result", "tool_use_id": call_id, "content": "..."}

    rows: list[tuple[UUID | None, str, str]] = [
        (sent_turn, "assistant", blocks(use("s0", "get_request_status"))),
        (sent_turn, "user", blocks(result("s0"))),
        (sent_turn, "assistant", blocks(use("card", "send_network_request"))),
        (sent_turn, "user", blocks(result("card"))),
        (sent_turn, "assistant", blocks(use("s1", "get_request_status"))),
        (sent_turn, "user", blocks(result("s1"))),
        (declined_turn, "assistant", blocks(use("card", "send_network_request"))),
        (declined_turn, "user", blocks(result("card"))),
        (declined_turn, "assistant", blocks(use("s2", "get_request_status"))),
    ]

    assert status_checks_after_send(rows, {(sent_turn, "card")}) == 1


# --- The worker's activity log --------------------------------------------------------


def failed(
    error: BaseException,
    *,
    queue: str = MODEL_TASK_QUEUE,
    workflow: str = "w:1",
    attempt: int = 1,
    log: ActivityLog | None = None,
) -> bool:
    """Log `error` as temporalio logs a failed activity attempt; whether it was kept."""
    record = logging.LogRecord(
        "temporalio.activity",
        logging.WARNING,
        __file__,
        1,
        "Completing activity as failed",
        None,
        (type(error), error, None),
    )
    record.temporal_activity = {"workflow_id": workflow, "task_queue": queue, "attempt": attempt}
    return (log or LOG).filter(record)


LOG = ActivityLog()


def test_the_activity_log_keeps_model_calls_that_failed_for_good() -> None:
    LOG.failures.clear()
    assert failed(BadRequest("no cassette entry"), workflow="w:a")
    assert failed(BadRequest("again"), workflow="w:a")
    assert failed(ApplicationError("budget", type="BudgetExhausted"), workflow="w:b")
    assert failed(RuntimeError("overloaded, retried"), workflow="w:c")
    assert failed(BadRequest("a database activity"), queue=MAIN_TASK_QUEUE, workflow="w:d")
    assert LOG.failures == {
        "w:a": "BadRequest: no cassette entry",
        "w:b": "BudgetExhausted: budget",
    }


def test_the_activity_log_counts_failed_attempts_by_kind_and_retries() -> None:
    log = ActivityLog()
    failed(RuntimeError("overloaded"), workflow="w:a", log=log)
    failed(RuntimeError("overloaded"), workflow="w:a", attempt=2, log=log)
    # The last attempt of a retryable error is a failure, not a retry.
    failed(RuntimeError("overloaded"), workflow="w:b", attempt=MODEL_ATTEMPTS, log=log)
    refused = ApplicationError(
        "session cap", {"kind": "session"}, type=BUDGET_EXHAUSTED_ERROR, non_retryable=True
    )
    failed(refused, queue=CHAT_TASK_QUEUE, workflow="w:c", log=log)
    failed(ApplicationError("cut off", type="TurnIncomplete"), workflow="w:d", log=log)
    failed(BadRequest("a database activity"), queue=MAIN_TASK_QUEUE, log=log)
    assert log.errors == {
        "RuntimeError": 3,
        "BudgetExhausted (session)": 1,
        "TurnIncomplete": 1,
    }
    assert log.retries == 2
    assert log.failures == {
        "w:b": "RuntimeError: overloaded",
        "w:c": "BudgetExhausted: session cap",
        "w:d": "TurnIncomplete: cut off",
    }
    lines = "\n".join(failure_lines(1, {"demo_budget": 1}, log))
    assert "refusals: 1" in lines
    assert "threads stopped short: demo_budget 1" in lines
    assert "TurnIncomplete: 1" in lines
    assert "BudgetExhausted: BudgetExhausted (session) 1" in lines
    assert "model retries: 2" in lines
    assert "n/a (external worker)" in "\n".join(failure_lines(0, {}, None))


def test_the_activity_log_drops_a_retired_worlds_failures() -> None:
    LOG.failures.clear()
    assert not failed(WorldRetired(uuid4()))
    assert not failed(ApplicationError("world is not live", type="WorldRetired"))
    assert LOG.failures == {}
    # Once the run retires its world, whatever fails there is the retirement's doing.
    world = uuid4()
    in_world = conversation_workflow_id(world, uuid4())
    assert failed(RuntimeError("row gone"), workflow=in_world)
    LOG.retiring(world)
    assert not failed(RuntimeError("row gone"), workflow=in_world)
    assert not failed(BadRequest("no entry"), workflow=in_world)
    assert failed(RuntimeError("elsewhere"), workflow=conversation_workflow_id(uuid4(), uuid4()))
    assert LOG.failures == {}
    plain = logging.LogRecord(
        "temporalio.activity", logging.INFO, __file__, 1, "Heartbeat", None, None
    )
    assert LOG.filter(plain)


# --- The poller check -----------------------------------------------------------------


@dataclass
class FakeService:
    pollers: list[PollerInfo]

    async def describe_task_queue(self, request: object) -> DescribeTaskQueueResponse:
        return DescribeTaskQueueResponse(pollers=self.pollers)


@dataclass
class FakeClient:
    workflow_service: FakeService
    namespace: str = "default"


def poller(name: str, seen: datetime) -> PollerInfo:
    stamp = Timestamp()
    stamp.FromDatetime(seen)
    return PollerInfo(identity=name, last_access_time=stamp)


async def test_only_recent_pollers_count() -> None:
    now = datetime.now(UTC)
    service = FakeService(
        [poller("dev-worker", now - timedelta(seconds=5)), poller("old", now - timedelta(hours=1))]
    )
    found = await active_pollers(cast(Client, FakeClient(service)), ["harmony-main"], now=now)
    assert found == ["dev-worker on harmony-main"]


def test_a_poller_from_an_exited_local_process_is_stale() -> None:
    host = socket.gethostname()
    assert not exited(f"{os.getpid()}@{host} harmony-worker abc")
    assert exited(f"{2**22 + 7}@{host} harmony-run")
    assert not exited(f"{2**22 + 7}@elsewhere harmony-run")
    assert not exited("dev-worker")


# --- The command ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["atlantis"], "atlantis"),
        (["harmony-demo", "--story", "other"], "has story 'suggested'"),
        (["harmony-demo", "--live", "--worker", "external"], "drop --worker external"),
        (["harmony-demo", "--worker", "elsewhere"], "in-process or external"),
        (["harmony-demo", "--as", "maya"], "the cassettes play alex's chat (chat:alex:1)"),
        (["harmony-demo", "--as", "helen", "--live"], "the story is Henderson Lab's request"),
        (["harmony-demo", "--as", "nobody"], "play it as one of alex, maya"),
        (["harmony-demo", "--message", "Book it by {someday}"], "--message:"),
        (["harmony-demo", "--message", "Hi", "--as", "nobody"], "no member 'nobody'"),
        (["harmony-demo", "--approve", "some"], "--approve is story or all"),
        (["harmony-demo", "--interactive", "--approve", "all"], "drop --approve all"),
        (["harmony-demo", "--edit", "nobody:henderson:2:Shorter."], "--edit: no org 'nobody'"),
        (["harmony-demo", "--edit", "ridgeline:henderson:Shorter."], "isn't ORG:COUNTERPARTY:N"),
    ],
)
def test_setup_errors_exit_2_before_anything_starts(args: list[str], message: str) -> None:
    result = CliRunner().invoke(cli, ["run", *args])
    assert result.exit_code == 2, result.output
    assert message in " ".join(result.output.split())


def test_run_help_lists_its_options() -> None:
    # Wide enough that rich doesn't cut the longest option name short.
    result = CliRunner().invoke(cli, ["run", "--help"], env={"COLUMNS": "160"})
    assert result.exit_code == 0
    for option in (
        "--story",
        "--live",
        "--as",
        "--message",
        "--approve",
        "--send-without-confirmation",
        "--interactive",
        "--worker",
        "--keep",
        "--edit",
    ):
        assert option in result.output


def test_a_message_plays_as_a_story_with_no_beats() -> None:
    options = RunOptions(
        scenario="harmony-demo",
        story="suggested",
        live=False,
        member="helen",
        interactive=False,
        worker="in-process",
        timeout_s=60,
        keep=False,
        message="Find SEM time on {+3d}.",
    )
    _, story = load_story(options)
    assert (story.member, story.suggested_message, story.steps, story.humans) == (
        "helen",
        "Find SEM time on {+3d}.",
        [],
        [],
    )
    # Without --as, the scenario story's member sends it; on the cassettes too.
    _, story = load_story(replace(options, member=None))
    assert story.member == STORY.member
