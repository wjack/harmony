"""`harmony run`'s pieces without a server: the play loop against a fake API that
plays the suggested story as the backend's contract says it would, the story's
decisions and recasting, the report, the worker's activity-failure log, the poller
check, and the command's setup errors."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from temporalio.api.taskqueue.v1 import PollerInfo
from temporalio.api.workflowservice.v1 import DescribeTaskQueueResponse
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from typer.testing import CliRunner

from harmony.agents.models import (
    AssistantTextItem,
    CardRecipient,
    ChatItem,
    InlineQuestionItem,
    MemberRef,
    OutreachCardItem,
    UserTextItem,
)
from harmony.api.routes.cards import ApproveCard
from harmony.api.routes.conversations import ConversationCreated, ConversationDetail
from harmony.api.routes.escalations import ResolveEscalation
from harmony.api.routes.inbox import Inbox, InboxEscalation, InboxItem
from harmony.api.routes.threads import ThreadDetail, ThreadMessageView
from harmony.api.routes.workspace import EscalationDraftView, EscalationView, ThreadWorkspace
from harmony.auth.views import MemberDirectory, MemberView, OrgRef, SessionView, WorldView
from harmony.cli import app as cli
from harmony.db.models.choices import EscalationKind, Fit, ThreadMessageKind, ThreadState
from harmony.domain.requests import RecipientStatus, RequestStatusView, WaitingOn
from harmony.errors import WorldRetired
from harmony.llm.errors import BadRequest
from harmony.seeds.loader import load_scenario
from harmony.seeds.schema import InlineQuestion, Resolved, Routed, StorySpec
from harmony.storyrun.api import ApiError, HarmonyApi
from harmony.storyrun.beats import recast
from harmony.storyrun.decide import (
    NO_DRAFT_TEXT,
    PendingEscalation,
    PromptDecisions,
    StoryDecisions,
)
from harmony.storyrun.play import StoryPlayer
from harmony.storyrun.report import beat_lines, chat_lines, thread_lines
from harmony.storyrun.worker import ActivityLog, active_pollers, exited
from harmony.temporal.policies import MAIN_TASK_QUEUE, MODEL_TASK_QUEUE
from harmony.temporal.workflows.payloads import (
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

    async def start_conversation(self, text: str) -> ConversationCreated:
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
            turn_failed=False,
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
        participant = "closed" if slug in self.ended else "needs_human" if open_here else "replied"
        return RecipientStatus(
            org=org_ref(slug),
            fit=CARD_FITS[slug],
            reason="Fits.",
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
            ),
            member=member_view(self.acting),
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


def card(fits: dict[str, Fit]) -> OutreachCardItem:
    return OutreachCardItem(
        id="card:1",
        at=NOW,
        card_id=uuid4(),
        status="pending",
        brief=BRIEF,
        recipients=[
            CardRecipient(org=org_ref(slug), fit=fit, reason="Fits.") for slug, fit in fits.items()
        ],
        screened=None,
        feedback=None,
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
        ApproveCard()
    )
    assert await decide.outreach(card({"calder": "no"})) == ApproveCard(), "never none"


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
        version=1,
        side_to_move=None,
        deadline_at=None,
        created_at=NOW,
        messages=[line(1, "message", "Booked: $1,410."), line(2, "close", "")],
        access=[],
    )
    text = "\n".join(thread_lines(thread))
    assert "Booked: $1,410." in text
    assert "2. Northlake Characterization Core closed the conversation (close)." in text


# --- The worker's activity log --------------------------------------------------------


def failed(error: BaseException, *, queue: str = MODEL_TASK_QUEUE, workflow: str = "w:1") -> bool:
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
    record.temporal_activity = {"workflow_id": workflow, "task_queue": queue, "attempt": 1}
    return LOG.filter(record)


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
    ],
)
def test_setup_errors_exit_2_before_anything_starts(args: list[str], message: str) -> None:
    result = CliRunner().invoke(cli, ["run", *args])
    assert result.exit_code == 2, result.output
    assert message in " ".join(result.output.split())


def test_run_help_lists_its_options() -> None:
    result = CliRunner().invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    for option in ("--story", "--live", "--as", "--interactive", "--worker", "--keep"):
        assert option in result.output
