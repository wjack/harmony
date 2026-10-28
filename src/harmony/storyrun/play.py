"""The loop that plays a story in a fresh world, through the API as a browser would.

It starts a session in the scenario, sends the story's message as its member (in a
chat that sends without review, if asked), then polls: the chat (outreach cards,
inline questions), the status of every request the chat sent, and the inboxes of the
people a thread may be waiting on. Where a person would act it asks `Decisions`, and it
records each beat the story's steps `expect` as it sees it.

Each `--edit` (`storyrun.edits`) is posted as a member of its org, an admin first, once
its thread has its messages.

A story stops when every expected beat has been seen. It stops early when it can no
longer finish: every request ended, a conversation the story needs closed expired or
was never opened, a model call in the story's own workflows failed for good, or the
worker stopped. A story with no beats (`--message`) is done once every request it sent
has closed and the chat is idle, and ends if Claude's turn ends without sending one.
Otherwise the run stops at the timeout. `settle` then waits for the requests to close
and Claude to sum them up in the chat.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Final
from uuid import UUID

from harmony.agents.models import (
    AssistantTextItem,
    InlineQuestionItem,
    MemberRef,
    OutreachCardItem,
    RequestStatusItem,
)
from harmony.api.routes.cards import ApproveCard
from harmony.api.routes.conversations import ConversationDetail
from harmony.api.routes.threads import ThreadDetail
from harmony.api.routes.workspace import AddSteer, ThreadWorkspace
from harmony.auth.views import MemberView, SessionView
from harmony.db.models.choices import MemberRole
from harmony.domain.requests import RequestStatusView
from harmony.domain.threads import TERMINAL_STATES
from harmony.seeds.dates import render_dates
from harmony.seeds.loader import Scenario
from harmony.seeds.schema import StorySpec
from harmony.storyrun.api import ApiError, HarmonyApi
from harmony.storyrun.beats import Atom, Outcome, SeenBeat, StoryRun, describe, expected_beats
from harmony.storyrun.decide import Decisions, PendingEscalation
from harmony.storyrun.edits import PlannedEdit
from harmony.temporal.workflows.payloads import (
    conversation_workflow_id,
    request_workflow_id,
    thread_workflow_id,
)

POLL_S = 0.5
TIMEOUT_S = 600.0
# Called with the world on every poll (the run's spend meter).
type PollHook = Callable[[UUID], Awaitable[None]]
# After the last beat, how long `settle` waits for the request to close and Claude's
# summary.
SETTLE_S = 60.0
FORBIDDEN: Final = 403
CONFLICT: Final = 409
# Who is read first among an org's members: admins can act on anything.
_ROLE_ORDER: Final[dict[MemberRole, int]] = {"admin": 0, "member": 1}

type Stop = tuple[Outcome, str | None]


class StoryPlayer:
    def __init__(
        self,
        api: HarmonyApi,
        scenario: Scenario,
        story: StorySpec,
        decisions: Decisions,
        *,
        log: Callable[[str], None] = print,
        poll_s: float = POLL_S,
        timeout_s: float = TIMEOUT_S,
        settle_s: float = SETTLE_S,
        worker: asyncio.Future[None] | None = None,
        failures: Mapping[str, str] | None = None,
        send_without_review: bool = False,
        on_poll: PollHook | None = None,
        edits: Sequence[PlannedEdit] = (),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """`story` is played as written; `beats.recast` plays it as someone else.
        `failures`: workflow ID → a model call in it that failed for good, as the
        in-process worker reports them (`worker.ActivityLog`). `send_without_review`
        starts the chat with that override, as the composer would. `edits` are posted
        as their threads reach them."""
        self._api = api
        self._scenario = scenario
        self._story = story
        self._decisions = decisions
        self._log = log
        self._poll_s = poll_s
        self._timeout_s = timeout_s
        self._settle_s = settle_s
        self._worker = worker
        # The worker's live map, empty when the run starts: never swapped for a copy.
        self._failures: Mapping[str, str] = failures if failures is not None else {}
        self._send_without_review = send_without_review
        self._on_poll = on_poll
        # Not posted yet.
        self._edits = list(edits)
        self._clock = clock
        self._sleep = sleep
        self._started = clock()
        self._members: dict[str, MemberView] = {}
        self._acting: UUID | None = None
        # Escalations settled, or left to the chat.
        self._handled: set[UUID] = set()
        # (escalation, member) pairs already tried from an inbox.
        self._tried: set[tuple[UUID, UUID]] = set()
        # Set once the session's world exists; whoever runs the player retires it.
        self.world_id: UUID | None = None

    @property
    def member(self) -> MemberView:
        return self._members[self._story.member]

    async def play(self) -> StoryRun:
        """Play the story in a new world. Raises only if the world can't be started;
        anything after that ends the run with its outcome."""
        session = await self._api.start_session(self._scenario.name)
        self.world_id = session.world_id
        run = StoryRun(world_id=session.world_id, expected=expected_beats(self._story))
        self._log(f"world {session.world_id} ({self._scenario.name}, a {session.world.mode} world)")
        try:
            await self._open(run, session)
            await self._loop(run)
        except ApiError as error:
            run.outcome, run.detail = "failed", str(error)
        for edit in self._edits:
            self._log(f"--edit {edit.describe()} was never posted")
        return run

    async def settle(self, run: StoryRun) -> bool:
        """After the last beat, wait (within `settle_s`) for the request to close and
        Claude to sum it up in the chat: reporting sooner would show the request still
        open, and retiring sooner would cut those last turns short. False if they didn't
        come in time."""
        deadline = self._clock() + self._settle_s
        while True:
            try:
                if await self._summarized(run):
                    return True
            except ApiError as error:
                self._log(f"settling: {error}")
                return False
            if self._clock() >= deadline:
                return False
            await self._sleep(self._poll_s)

    async def transcripts(
        self, run: StoryRun
    ) -> tuple[ConversationDetail | None, list[ThreadDetail]]:
        """The chat and each thread, as the story's member sees them now."""
        chat: ConversationDetail | None = None
        threads: list[ThreadDetail] = []
        if not self._members:
            return chat, threads
        try:
            await self._act_as(self.member.id)
            if run.conversation_id is not None:
                chat = await self._api.conversation(run.conversation_id)
            for thread_id, org in run.threads.items():
                try:
                    threads.append(await self._api.thread(thread_id))
                except ApiError as error:
                    self._log(f"thread with {org}: {error}")
        except ApiError as error:
            self._log(f"transcripts: {error}")
        return chat, threads

    async def _open(self, run: StoryRun, session: SessionView) -> None:
        directory = await self._api.members()
        self._members = {m.slug: m for m in [*directory.featured, *directory.all]}
        if self._story.member not in self._members:
            raise ApiError("GET", "/members", 200, f"no member {self._story.member!r} in the world")
        self._acting = session.member.id
        await self._act_as(self.member.id)
        # The anchor, not the world's date: the story's dates sit beside the seeds'.
        text = render_dates(self._story.suggested_message, self._scenario.anchor)
        self._log(f"{self.member.name}: {text}")
        created = await self._api.start_conversation(
            text, send_without_confirmation=True if self._send_without_review else None
        )
        run.conversation_id = created.id

    async def _loop(self, run: StoryRun) -> None:
        deadline = self._started + self._timeout_s
        while True:
            if self._worker is not None and self._worker.done():
                run.outcome, run.detail = "failed", "the worker stopped"
                return
            if self._on_poll is not None:
                await self._on_poll(run.world_id)
            stop = await self._tick(run)
            if run.complete and run.expected:
                run.outcome = "done"
                return
            # A failed model call explains a thread that ended, so it goes first. A
            # story with no beats follows its requests to the end however they fare.
            if run.expected:
                stop = self._failed_model_call(run) or stop
            if stop is not None:
                run.outcome, run.detail = stop
                return
            if self._clock() >= deadline:
                run.outcome, run.detail = "timeout", f"no progress after {self._timeout_s:.0f}s"
                return
            await self._sleep(self._poll_s)

    async def _tick(self, run: StoryRun) -> Stop | None:
        """One poll. How the run ends, if it does: a story that can't finish any more,
        or one with no beats whose requests are over."""
        assert run.conversation_id is not None
        await self._act_as(self.member.id)
        chat = await self._api.conversation(run.conversation_id)
        await self._chat(run, chat)
        statuses: list[RequestStatusView] = []
        for request_id in list(run.request_ids):
            await self._act_as(self.member.id)
            status = await self._api.request(request_id)
            statuses.append(status)
            self._note_threads(run, status)
            for member in self._inboxes(status):
                await self._inbox(run, member)
        await self._post_edits(run)
        if not run.expected:
            return _free_form_end(chat, statuses)
        if statuses and all(status.status == "closed" for status in statuses):
            return "ended", "every request closed before the story finished"
        return self._dead_end(run, statuses)

    # --- The chat ---------------------------------------------------------------------

    async def _chat(self, run: StoryRun, chat: ConversationDetail) -> None:
        for item in chat.items:
            match item:
                case OutreachCardItem(status="pending", auto=False) if (
                    item.card_id not in self._handled
                ):
                    self._handled.add(item.card_id)
                    choice = await self._decisions.outreach(item)
                    if isinstance(choice, ApproveCard):
                        resolved = await self._unless_settled(
                            self._api.approve_card(item.card_id, choice), "outreach card"
                        )
                        if resolved is not None:
                            run.add_request(resolved.request_id)
                            self._see(run, ("outreach_approved",))
                    elif await self._unless_settled(
                        self._api.reject_card(item.card_id, choice.feedback), "outreach card"
                    ):
                        self._log(f"outreach card rejected: {choice.feedback}")
                case OutreachCardItem(status="approved", request_id=UUID() as request_id):
                    if item.auto:
                        # Sent without review: nobody approved it, but it went out.
                        self._see(run, ("outreach_approved",))
                    run.add_request(request_id)
                case RequestStatusItem(request_id=request_id):
                    run.add_request(request_id)
                case InlineQuestionItem(status="open") if item.escalation_id not in self._handled:
                    self._see(run, ("inline_question", item.from_org.slug, self.member.slug))
                    await self._inline_question(run, item)
                case _:
                    pass

    async def _inline_question(self, run: StoryRun, item: InlineQuestionItem) -> None:
        self._handled.add(item.escalation_id)
        workspace = await self._api.workspace(item.thread_id)
        pending = _pending(
            workspace,
            item.thread_id,
            item.escalation_id,
            org=self.member.org.slug,
            member=self._ref(self.member),
            counterparty=item.from_org.slug,
            question=item.question,
        )
        await self._resolve(run, pending)

    # --- Other members' inboxes -------------------------------------------------------

    def _inboxes(self, status: RequestStatusView) -> list[MemberView]:
        """Whose inboxes to read. `waiting_on` and `needs_human` cover only the requester's
        own escalations (whether the other side waits on a person is that side's to
        know), so every member is read of a recipient whose thread is still open. The
        story's `humans` rules decide what to do, not whom to read. The story's member
        answers in the chat instead."""
        wanted: dict[UUID, MemberView] = {}
        for recipient in status.recipients:
            waiting = recipient.waiting_on
            view = self._members.get(waiting.member.slug) if waiting is not None else None
            if view is not None:
                wanted[view.id] = view
            if recipient.participant in ("messaged", "replied", "needs_human"):
                for member in self._org_members(recipient.org.slug):
                    wanted.setdefault(member.id, member)
        wanted.pop(self.member.id, None)
        return list(wanted.values())

    def _org_members(self, org: str) -> list[MemberView]:
        """`org`'s members: those the story's rules name first, then by role."""
        named = {rule.member for rule in self._story.humans if rule.org == org}
        members = [m for m in self._members.values() if m.org.slug == org]
        return sorted(members, key=lambda m: (m.slug not in named, _ROLE_ORDER[m.role]))

    async def _inbox(self, run: StoryRun, member: MemberView) -> None:
        await self._act_as(member.id)
        inbox = await self._api.inbox()
        for item in inbox.needs_you:
            escalation = item.escalation
            if escalation is None or escalation.status != "open":
                continue
            # Another request's, such as a thread the scenario seeds.
            if item.thread_id not in run.threads:
                continue
            if escalation.id in self._handled or (escalation.id, member.id) in self._tried:
                continue
            self._tried.add((escalation.id, member.id))
            self._see(run, ("routed", member.org.slug, member.slug))
            workspace = await self._api.workspace(item.thread_id)
            pending = _pending(
                workspace,
                item.thread_id,
                escalation.id,
                org=member.org.slug,
                member=self._ref(member),
                counterparty=item.counterparty.slug,
                question=escalation.question,
                reason=escalation.reason,
            )
            if await self._resolve(run, pending):
                self._handled.add(escalation.id)

    async def _resolve(self, run: StoryRun, pending: PendingEscalation) -> bool:
        """Resolve `pending` as decided. False when its member may not (403), so another
        member can take it; True once it is settled, by them or by anyone."""
        body = await self._decisions.escalation(pending)
        try:
            resolved = await self._unless_settled(
                self._api.resolve(pending.escalation_id, body), "escalation"
            )
        except ApiError as error:
            if error.status != FORBIDDEN:
                raise
            self._log(f"  {pending.member.name} can't resolve it: {error.detail}")
            return False
        if resolved is not None:
            said = body.text or (pending.draft if body.action == "approve" else None)
            if said:
                self._log(f"  {pending.member.name}: {said}")
            self._see(run, ("resolved", pending.org, pending.member.slug, body.action))
        return True

    # --- Edits -----------------------------------------------------------------------

    async def _post_edits(self, run: StoryRun) -> None:
        """Post each edit whose thread has its messages. One whose thread ended first is
        dropped; one refused while another steer runs on that side (409) is tried again
        on the next poll."""
        requester = self.member.org.slug
        for edit in list(self._edits):
            thread_id = next(
                (
                    thread_id
                    for thread_id, responder in run.threads.items()
                    if {requester, responder} == {edit.org, edit.counterparty}
                ),
                None,
            )
            if thread_id is None:
                continue
            member = self._org_members(edit.org)[0]
            await self._act_as(member.id)
            thread = await self._api.thread(thread_id)
            if thread.state in TERMINAL_STATES:
                self._edits.remove(edit)
                self._log(f"--edit {edit.describe()}: the thread ended first")
                continue
            if len(thread.messages) < edit.after_messages:
                continue
            body = AddSteer(client_key=edit.client_key, as_member_id=member.id, body=edit.text)
            try:
                await self._api.steer(thread_id, body)
            except ApiError as error:
                if error.status != CONFLICT:
                    raise
                self._log(f"  --edit {edit.describe()}: {error.detail}; trying again")
                continue
            self._edits.remove(edit)
            elapsed = self._clock() - self._started
            self._log(f"[{elapsed:6.1f}s] {member.name} edits {edit.org}'s side: {edit.text}")

    # --- Settling --------------------------------------------------------------------

    async def _summarized(self, run: StoryRun) -> bool:
        """Every request closed, and Claude has written in the chat since their threads'
        last activity, with nothing in flight."""
        if not run.request_ids or run.conversation_id is None:
            return False
        await self._act_as(self.member.id)
        statuses = [await self._api.request(request_id) for request_id in run.request_ids]
        if any(status.status != "closed" for status in statuses):
            return False
        chat = await self._api.conversation(run.conversation_id)
        if chat.turn_in_flight or chat.queued_message_ids:
            return False
        last = max(
            (
                r.last_activity_at
                for status in statuses
                for r in status.recipients
                if r.last_activity_at is not None
            ),
            default=None,
        )
        return any(
            isinstance(item, AssistantTextItem) and (last is None or item.at > last)
            for item in chat.items
        )

    # --- When to stop early -----------------------------------------------------------

    def _dead_end(self, run: StoryRun, statuses: list[RequestStatusView]) -> Stop | None:
        """A conversation the story needs closed expired, or isn't on any request."""
        if not statuses:
            return None
        needed = {atom[1] for atom in run.missing_atoms() if atom[0] == "closed"}
        recipients = [recipient for status in statuses for recipient in status.recipients]
        for recipient in recipients:
            if recipient.org.slug in needed and recipient.thread_state == "expired":
                return "ended", (
                    f"the conversation with {recipient.org.name} expired before the story finished"
                )
        absent = sorted(needed - {recipient.org.slug for recipient in recipients})
        if absent:
            return "ended", f"the request doesn't include {', '.join(absent)}"
        return None

    def _failed_model_call(self, run: StoryRun) -> Stop | None:
        """A model call in one of the story's own workflows that failed for good: its
        turn is lost, so the story can't go on. Other worlds' and requests' don't count."""
        if not self._failures:
            return None
        world = run.world_id
        ids = {thread_workflow_id(world, thread_id) for thread_id in run.threads}
        if run.conversation_id is not None:
            ids.add(conversation_workflow_id(world, run.conversation_id))
        ids |= {request_workflow_id(world, request_id) for request_id in run.request_ids}
        failure = next((f for wid, f in self._failures.items() if wid in ids), None)
        return ("failed", f"a model call failed for good: {failure}") if failure else None

    # --- Bookkeeping ------------------------------------------------------------------

    def _note_threads(self, run: StoryRun, status: RequestStatusView) -> None:
        for recipient in status.recipients:
            if recipient.thread_id is not None:
                run.threads[recipient.thread_id] = recipient.org.slug
            if recipient.thread_state == "closed":
                self._see(run, ("closed", recipient.org.slug))

    async def _unless_settled[T](self, call: Awaitable[T], what: str) -> T | None:
        """`call`, or None when someone settled it first (409): another tab, another
        member, or the thread ending. Any other error stops the run."""
        try:
            return await call
        except ApiError as error:
            if error.status != CONFLICT:
                raise
            self._log(f"  {what} already settled: {error.detail}")
            return None

    def _see(self, run: StoryRun, atom: Atom) -> None:
        if atom in run.seen_atoms:
            return
        elapsed = self._clock() - self._started
        run.seen.append(SeenBeat(atom, elapsed))
        self._log(f"[{elapsed:6.1f}s] {describe(atom)}")

    async def _act_as(self, member_id: UUID) -> None:
        if self._acting != member_id:
            await self._api.act_as(member_id)
            self._acting = member_id

    @staticmethod
    def _ref(member: MemberView) -> MemberRef:
        return MemberRef(id=member.id, slug=member.slug, name=member.name)


def _free_form_end(chat: ConversationDetail, statuses: list[RequestStatusView]) -> Stop | None:
    """A story with no beats: done once every request it sent has closed and the chat
    is idle; over if Claude's turn ended without sending one."""
    idle = not (
        chat.turn_in_flight
        or chat.queued_message_ids
        or any(
            isinstance(item, OutreachCardItem) and item.status == "pending" for item in chat.items
        )
    )
    if not idle:
        return None
    if statuses:
        return ("done", None) if all(s.status == "closed" for s in statuses) else None
    if chat.turn_failure is not None:
        return "failed", f"Claude couldn't reply ({chat.turn_failure})"
    if any(isinstance(item, AssistantTextItem) for item in chat.items):
        return "ended", "Claude's turn ended without sending a request"
    return None


def _pending(
    workspace: ThreadWorkspace,
    thread_id: UUID,
    escalation_id: UUID,
    *,
    org: str,
    member: MemberRef,
    counterparty: str,
    question: str,
    reason: str | None = None,
) -> PendingEscalation:
    """The escalation as its resolver sees it, with the draft they'd edit."""
    escalation = next((e for e in workspace.escalations if e.id == escalation_id), None)
    draft = None
    if escalation is not None and escalation.drafts:
        rev = escalation.current_draft_rev
        draft = next((d for d in escalation.drafts if d.draft_rev == rev), escalation.drafts[-1])
    if escalation is not None and reason is None:
        reason = escalation.reason
    return PendingEscalation(
        escalation_id=escalation_id,
        kind=escalation.kind if escalation is not None else "consult",
        org=org,
        member=member,
        counterparty=counterparty,
        thread_id=thread_id,
        question=question,
        reason=reason,
        draft=draft.answer if draft is not None else None,
        draft_rev=draft.draft_rev if draft is not None else None,
    )
