"""The loop that plays a story in a fresh world, through the API as a browser would.

It starts a session in the scenario, sends the story's message as its member, then
polls: the chat (the outreach card, inline questions), the request's status, and the
inboxes of the people a thread may be waiting on. Where a person would act it asks
`Decisions`, and it records each beat the story's steps `expect` as it sees it.

It stops when every expected beat has been seen. It stops early when the story can no
longer finish: the request ended, a conversation the story needs closed expired or was
never opened, a model call in the story's own workflows failed for good, or the worker
stopped. Otherwise it stops at the timeout. `settle` then waits for the request to
close and Claude to sum it up in the chat.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
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
from harmony.api.routes.workspace import ThreadWorkspace
from harmony.auth.views import MemberView, SessionView
from harmony.db.models.choices import MemberRole
from harmony.domain.requests import RequestStatusView
from harmony.seeds.dates import render_dates
from harmony.seeds.loader import Scenario
from harmony.seeds.schema import StorySpec
from harmony.storyrun.api import ApiError, HarmonyApi
from harmony.storyrun.beats import Atom, Outcome, SeenBeat, StoryRun, describe, expected_beats
from harmony.storyrun.decide import Decisions, PendingEscalation
from harmony.temporal.workflows.payloads import (
    conversation_workflow_id,
    request_workflow_id,
    thread_workflow_id,
)

POLL_S = 0.5
TIMEOUT_S = 600.0
# After the last beat, how long `settle` waits for the request to close and Claude's
# summary.
SETTLE_S = 60.0
FORBIDDEN: Final = 403
CONFLICT: Final = 409
# Who is read first among an org's members: admins can act on anything.
_ROLE_ORDER: Final[dict[MemberRole, int]] = {"admin": 0, "member": 1}

type Stop = tuple[Outcome, str]


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
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """`story` is played as written; `beats.recast` plays it as someone else.
        `failures`: workflow ID → a model call in it that failed for good, as the
        in-process worker reports them (`worker.ActivityLog`)."""
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
            for org, thread_id in run.threads.items():
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
        created = await self._api.start_conversation(text)
        run.conversation_id = created.id

    async def _loop(self, run: StoryRun) -> None:
        deadline = self._started + self._timeout_s
        while True:
            if self._worker is not None and self._worker.done():
                run.outcome, run.detail = "failed", "the worker stopped"
                return
            stop = await self._tick(run)
            if run.complete and run.expected:
                run.outcome = "done"
                return
            # A failed model call explains a thread that ended, so it goes first.
            stop = self._failed_model_call(run) or stop
            if stop is not None:
                run.outcome, run.detail = stop
                return
            if self._clock() >= deadline:
                run.outcome, run.detail = "timeout", f"no progress after {self._timeout_s:.0f}s"
                return
            await self._sleep(self._poll_s)

    async def _tick(self, run: StoryRun) -> Stop | None:
        """One poll. Why the story can't finish any more, if it can't."""
        assert run.conversation_id is not None
        await self._act_as(self.member.id)
        chat = await self._api.conversation(run.conversation_id)
        await self._chat(run, chat)
        if run.request_id is None:
            return None
        status = await self._api.request(run.request_id)
        self._note_threads(run, status)
        for member in self._inboxes(status):
            await self._inbox(run, member)
        if status.status == "closed":
            return "ended", "the request was closed before the story finished"
        return self._dead_end(run, status)

    # --- The chat ---------------------------------------------------------------------

    async def _chat(self, run: StoryRun, chat: ConversationDetail) -> None:
        for item in chat.items:
            match item:
                case OutreachCardItem(status="pending") if item.card_id not in self._handled:
                    self._handled.add(item.card_id)
                    choice = await self._decisions.outreach(item)
                    if isinstance(choice, ApproveCard):
                        resolved = await self._unless_settled(
                            self._api.approve_card(item.card_id, choice), "outreach card"
                        )
                        if resolved is not None:
                            run.request_id = run.request_id or resolved.request_id
                            self._see(run, ("outreach_approved",))
                    elif await self._unless_settled(
                        self._api.reject_card(item.card_id, choice.feedback), "outreach card"
                    ):
                        self._log(f"outreach card rejected: {choice.feedback}")
                case OutreachCardItem(status="approved", request_id=UUID() as request_id):
                    run.request_id = run.request_id or request_id
                case RequestStatusItem(request_id=request_id):
                    run.request_id = run.request_id or request_id
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
        """Whose inboxes to read. `waiting_on` names only the requester's own assignees
        (who the other side waits on is that side's to know), so every member is read
        of a recipient whose thread needs a human. The story's `humans` rules decide
        what to do, not whom to read. The story's member answers in the chat instead."""
        wanted: dict[UUID, MemberView] = {}
        for recipient in status.recipients:
            waiting = recipient.waiting_on
            view = self._members.get(waiting.member.slug) if waiting is not None else None
            if view is not None:
                wanted[view.id] = view
            if recipient.participant == "needs_human":
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
            if item.thread_id not in run.threads.values():
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

    # --- Settling --------------------------------------------------------------------

    async def _summarized(self, run: StoryRun) -> bool:
        """The request closed, and Claude has written in the chat since its threads'
        last activity, with nothing in flight."""
        if run.request_id is None or run.conversation_id is None:
            return False
        await self._act_as(self.member.id)
        status = await self._api.request(run.request_id)
        if status.status != "closed":
            return False
        chat = await self._api.conversation(run.conversation_id)
        if chat.turn_in_flight or chat.queued_message_ids:
            return False
        last = max(
            (r.last_activity_at for r in status.recipients if r.last_activity_at is not None),
            default=None,
        )
        return any(
            isinstance(item, AssistantTextItem) and (last is None or item.at > last)
            for item in chat.items
        )

    # --- When to stop early -----------------------------------------------------------

    def _dead_end(self, run: StoryRun, status: RequestStatusView) -> Stop | None:
        """A conversation the story needs closed expired, or isn't on the request."""
        needed = {atom[1] for atom in run.missing_atoms() if atom[0] == "closed"}
        for recipient in status.recipients:
            if recipient.org.slug in needed and recipient.thread_state == "expired":
                return "ended", (
                    f"the conversation with {recipient.org.name} expired before the story finished"
                )
        absent = sorted(needed - {recipient.org.slug for recipient in status.recipients})
        if absent:
            return "ended", f"the request doesn't include {', '.join(absent)}"
        return None

    def _failed_model_call(self, run: StoryRun) -> Stop | None:
        """A model call in one of the story's own workflows that failed for good: its
        turn is lost, so the story can't go on. Other worlds' and requests' don't count."""
        if not self._failures:
            return None
        world = run.world_id
        ids = {thread_workflow_id(world, thread_id) for thread_id in run.threads.values()}
        if run.conversation_id is not None:
            ids.add(conversation_workflow_id(world, run.conversation_id))
        if run.request_id is not None:
            ids.add(request_workflow_id(world, run.request_id))
        failure = next((f for wid, f in self._failures.items() if wid in ids), None)
        return ("failed", f"a model call failed for good: {failure}") if failure else None

    # --- Bookkeeping ------------------------------------------------------------------

    def _note_threads(self, run: StoryRun, status: RequestStatusView) -> None:
        for recipient in status.recipients:
            if recipient.thread_id is not None:
                run.threads[recipient.org.slug] = recipient.thread_id
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
