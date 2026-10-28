"""Workflow inputs, results, and signal / Update / query payloads for P1–P2 (§7).

Kept apart from the workflow classes so activities, the API and tests import them
without importing a workflow definition. Pure Pydantic: safe in the workflow sandbox
(the workflows import this module through `imports_passed_through`).

Workflow IDs (`world_workflow_id`, so retirement finds them):
- `ConversationWorkflow`: `w:{world}:conv:{conversation_id}`;
- `NetworkRequestWorkflow`: `w:{world}:req:{request_id}`;
- `ThreadWorkflow`: `w:{world}:thread:{thread_id}`.

Errors an Update validator raises reach the API as `WorkflowUpdateFailedError` whose
cause is an `ApplicationError` with one of the `*_ERROR` types below; the API maps them
to its typed problems (`api.routes.problems`).

Owner: E4.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from hashlib import sha256
from types import MappingProxyType
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from temporalio.exceptions import ActivityError, ApplicationError

from harmony.agents.models import MAX_ACTIVE_THREADS, EscalationAction, RequestStatus
from harmony.agents.runtime import OwedResult, TurnStop
from harmony.db.models.choices import (
    BudgetKind,
    CardStatus,
    FailureReason,
    Side,
    ThreadState,
)
from harmony.temporal.start import world_workflow_id

CONVERSATION_KIND: Final = "conv"
REQUEST_KIND: Final = "req"
THREAD_KIND: Final = "thread"

# `ApplicationError.type`s raised by Update validators and handlers.
CARD_NOT_PENDING_ERROR: Final = "CardNotPending"
ESCALATION_NOT_OPEN_ERROR: Final = "EscalationNotOpen"
# The acting member isn't in the escalation's org (403).
NOT_IN_ORG_ERROR: Final = "NotInOrg"
# A resolution or card decision missing what its action needs (422).
INVALID_RESOLUTION_ERROR: Final = "InvalidResolution"
# The row an Update names isn't in the world (404).
NOT_FOUND_ERROR: Final = "NotFoundInWorld"
# A steer is already running for that side (409 `steer_running`), with
# `details=[{"by_member_id": <its author>}]`.
STEER_RUNNING_ERROR: Final = "SteerRunning"
# The thread has ended, or is ending: Reopen is the way back (409 `thread_closed`).
THREAD_CLOSED_ERROR: Final = "ThreadClosed"
# A steer id already stored with different content: one client reused its key for
# different text (422 `invalid`).
STEER_CONFLICT_ERROR: Final = "SteerConflict"
# A model activity's refused hosted reservation, non-retryable, with
# `details=[BudgetExhaustedDetails(...).model_dump()]`: an uncaught exception would
# reach the workflow with only its class name and message (3.5).
BUDGET_EXHAUSTED_ERROR: Final = "BudgetExhausted"

# Which failure a refused reservation is, by the cap that refused it (3.5).
BUDGET_FAILURES: Final[Mapping[BudgetKind, FailureReason]] = MappingProxyType(
    {
        "session": "session_budget",
        "client": "daily_budget",
        "daily": "daily_budget",
        "global": "demo_budget",
        "provider": "demo_budget",
        "paused": "paused",
    }
)

# A steer's instruction is as long as a comment may be (`notes.BODY_MAX_CHARS`), and the
# unsaved draft wording it carries at most `steers.draft_text`'s CHECK.
STEER_BODY_MAX_CHARS: Final = 4000
DRAFT_TEXT_MAX_CHARS: Final = 20_000

# A conversation workflow continues as new after this many turns, and exits after
# this long without a message, an update or a pending card.
CONVERSATION_TURNS_PER_RUN: Final = 50
CONVERSATION_IDLE: Final = timedelta(hours=1)
# An update look (`ConversationWorkflow`) waits for this long a lull after the latest
# counterparty news, runs at most once per `LOOK_EVERY`, and never later than
# `LOOK_LATEST` after the first news it hasn't looked at.
LOOK_LULL: Final = timedelta(seconds=30)
LOOK_EVERY: Final = timedelta(seconds=60)
LOOK_LATEST: Final = timedelta(minutes=3)


def conversation_workflow_id(world_id: UUID, conversation_id: UUID) -> str:
    return world_workflow_id(world_id, CONVERSATION_KIND, conversation_id)


def request_workflow_id(world_id: UUID, request_id: UUID) -> str:
    return world_workflow_id(world_id, REQUEST_KIND, request_id)


def thread_workflow_id(world_id: UUID, thread_id: UUID) -> str:
    return world_workflow_id(world_id, THREAD_KIND, thread_id)


def update_failure(error: ActivityError) -> ApplicationError:
    """An activity failure inside an Update handler, as the `ApplicationError` that
    fails the Update (and only it): the activity's error type and details carry through
    to the API, which maps them to problems."""
    cause = error.cause
    if isinstance(cause, ApplicationError):
        return ApplicationError(cause.message, *cause.details, type=cause.type)
    return ApplicationError(str(error), type=type(cause).__name__ if cause else "ActivityError")


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BudgetExhaustedDetails(_Payload):
    """The one detail of a `BUDGET_EXHAUSTED_ERROR`."""

    kind: BudgetKind


# --- ConversationWorkflow ------------------------------------------------------------


class MemberMessage(_Payload):
    """Signal `member_message`: a member typed in the chat. The API has stored nothing;
    the next `internal_turn` appends it to the transcript (`message_id` is its row id)."""

    message_id: UUID
    member_id: UUID
    text: str = Field(min_length=1, max_length=20_000)
    sent_at: datetime
    # The card pending when the member sent it, which the message answers (rejects
    # with its text as feedback); None when no card was pending, so it waits for the
    # next turn even if the turn in flight raises one meanwhile.
    card_id: UUID | None = None


# `closed`: every thread of the request has ended, so Claude can tell the member how it
# went. An inline question is no update: the chat refetches from the escalation's commit.
NetworkUpdateKind = Literal["closed"]


class NetworkUpdate(_Payload):
    """Signal `network_update`: something on one of this chat's requests that internal
    Claude should react to. Sent by the network workflows' `notify_conversation`
    activity (signal-with-start, so a closed conversation run wakes up)."""

    request_id: UUID
    kind: NetworkUpdateKind
    # The request's round that ended: each reopen that reopened the request adds one
    # (REOPEN.md §6.2), and the chat takes each round once.
    round: int = Field(default=1, ge=1)


class NetworkNews(_Payload):
    """Signal `network_news`: a counterparty wrote on one of this chat's requests, or a
    conversation on one ended (`NetworkActivities.nudge_chat`), or the chat's owner
    answered their own representative's question on one (the resolve route), all by
    signal-with-start. It arms an update look and never starts a turn itself."""

    request_id: UUID
    # The owner answered: the look is due at once, so Claude can ask about passing part
    # of the answer on.
    answered: bool = False


class ConversationCarry(_Payload):
    """What a conversation run hands to the next one on continue-as-new."""

    queued_messages: list[MemberMessage] = []
    network_updates: list[NetworkUpdate] = []
    # Internal turns taken so far (the cassette turn number continues from here).
    turns: int = Field(default=0, ge=0)
    # Read only, from runs before rounds: requests whose round 1 `closed` was taken.
    closed_request_ids: list[UUID] = []
    # The highest round whose `closed` update was taken, per request: a repeat (a
    # retried signal, or a second caller of `end_round`) is dropped.
    closed_request_rounds: dict[UUID, int] = {}
    # `closed` updates a turn took but never stored (`ConversationWorkflow._undelivered`).
    undelivered_updates: list[NetworkUpdate] = []
    # Why the last turn failed, if it did and no turn has run since.
    turn_failure: FailureReason | None = None
    # Update looks taken (their cassette turn number continues from here; a look never
    # takes a chat turn number).
    looks: int = Field(default=0, ge=0)
    # The armed look: the first and the latest news it hasn't looked at.
    news_first_at: datetime | None = None
    news_latest_at: datetime | None = None
    # When the last look ran.
    looked_at: datetime | None = None
    # The armed look follows an answer of the owner's, so it is due at once.
    news_answered: bool = False


class ConversationInput(_Payload):
    world_id: UUID
    conversation_id: UUID
    org_id: UUID
    owner_member_id: UUID
    # The conversation's cassette thread key, `chat:<member>:<n>` (`agents.keys`).
    cassette_thread: str
    carried: ConversationCarry = ConversationCarry()
    # Continue as new at the first idle point after this many turns in one run.
    turns_per_run: int = Field(default=CONVERSATION_TURNS_PER_RUN, ge=1)


class ConversationResult(_Payload):
    """Returned when the run exits idle (the next message restarts it)."""

    turns: int


class CardResolved(_Payload):
    """`resolve_card`'s result."""

    card_id: UUID
    status: CardStatus
    # Set when approving the outreach card started a request.
    request_id: UUID | None = None


class ConversationPending(_Payload):
    """Query `pending`: what the workflow holds that isn't in the database yet."""

    queued_messages: list[MemberMessage]
    network_updates: list[NetworkUpdate]
    turn_in_flight: bool
    pending_card_id: UUID | None
    turns: int
    # Why the last turn failed (Claude couldn't reply), if no turn has run since.
    turn_failure: FailureReason | None = None
    # An update look is running: Claude is checking the news, and may say nothing.
    looking: bool = False


# --- NetworkRequestWorkflow ----------------------------------------------------------


class NetworkRequestInput(_Payload):
    world_id: UUID
    request_id: UUID
    requester_org_id: UUID
    conversation_id: UUID | None
    recipient_org_ids: list[UUID] = Field(min_length=1)
    max_active_threads: int = Field(default=MAX_ACTIVE_THREADS, ge=1)


class NetworkRequestResult(_Payload):
    status: RequestStatus


class ThreadChanged(_Payload):
    """Signal `thread_changed` from a `ThreadWorkflow`: its state moved."""

    thread_id: UUID
    responder_org_id: UUID
    state: ThreadState


class ThreadProgressRef(_Payload):
    thread_id: UUID
    responder_org_id: UUID
    state: ThreadState


class RequestProgress(_Payload):
    """Query `status`."""

    request_id: UUID
    status: RequestStatus
    threads: list[ThreadProgressRef]


# --- ThreadWorkflow ------------------------------------------------------------------


class ThreadCarry(_Payload):
    """What one run of a thread hands the next (REOPEN.md §4.9): turn numbers, delivered
    seqs and consult ordinals continue, and each side's next turn opens with the tool
    results it is owed, so its transcript stays valid across runs."""

    turns: dict[Side, int]
    delivered: dict[Side, int]
    consults: dict[Side, int]
    owed: dict[Side, list[OwedResult]]

    @field_validator("owed")
    @classmethod
    def _settled(cls, owed: dict[Side, list[OwedResult]]) -> dict[Side, list[OwedResult]]:
        # A pending result would wait forever: nothing in a new run resolves it.
        if any(result.content is None for results in owed.values() for result in results):
            raise ValueError("a carried tool result can't be pending")
        return owed


class ReopenInput(_Payload):
    """The attempt a reopened run starts with (REOPEN.md §4.3): the API recorded the
    steer, and `record_reopen` writes it again if that commit failed."""

    steer_id: UUID
    side: Side
    org_id: UUID
    member_id: UUID
    body: str = Field(min_length=1, max_length=STEER_BODY_MAX_CHARS)
    # The steer's seq, and the terminal state the thread is reopened from.
    seq: int = Field(ge=1)
    from_state: ThreadState


class ThreadInput(_Payload):
    world_id: UUID
    thread_id: UUID
    request_id: UUID
    requester_org_id: UUID
    responder_org_id: UUID
    # The thread's cassette key (`agents.keys.thread_key`), fixed when it opens.
    cassette_thread: str
    # Carried turns included, so every run gets the same number of new turns.
    max_turns: int = Field(default=16, ge=1)
    # The thread ends `expired` here, whatever it waits on (a person included), so it
    # always writes its own terminal state inside the 24 h execution timeout.
    deadline_at: datetime | None = None
    # Set on a reopened run (REOPEN.md §4.3); all defaulted, so run 1 is unchanged.
    run: int = Field(default=1, ge=1)
    carry: ThreadCarry | None = None
    reopen: ReopenInput | None = None
    # Each side's latest guidance version and steer seq, which the run starts from.
    guidance: dict[Side, int] = {}
    steer_seq: dict[Side, int] = {}


class ThreadResult(_Payload):
    state: ThreadState
    turns: int


class EscalationResolved(_Payload):
    """`resolve_escalation`'s result."""

    escalation_id: UUID
    action: EscalationAction
    # The thread message the resolution sent: the member's own (`take_over`), or the
    # held message approved (`outbound_review`). A released consult answer sends none.
    message_id: UUID | None = None


class Reassignment(_Payload):
    """Update `reassign`: move the item and its `routed` thread access to another
    member of the org. Nothing is recorded for routing to learn from (§6)."""

    escalation_id: UUID
    to_member_id: UUID
    by_member_id: UUID


class Reassigned(_Payload):
    escalation_id: UUID
    assignees: list[UUID]


class SteerRequest(_Payload):
    """Update `steer`: a member's Edit on this thread (EDIT.md §3.1). The API has checked
    who sent it and that they can see the thread and the item; the id is the server's
    (`steers.steer_id_for`), so the Update id (`update_id`) dedupes a retried POST."""

    steer_id: UUID
    side: Side
    org_id: UUID
    member_id: UUID
    client_key: UUID
    body: str = Field(min_length=1, max_length=STEER_BODY_MAX_CHARS)
    # The waiting item the member was looking at, if any.
    escalation_id: UUID | None = None
    # The member's unsaved wording of that item's draft, which the rewrite starts from.
    draft_text: str | None = Field(default=None, max_length=DRAFT_TEXT_MAX_CHARS)

    @property
    def update_id(self) -> str:
        """`steer:{steer_id}:{body digest}`. Temporal answers a repeated Update id with
        the first one's outcome, so the same key with other text must be a new Update:
        `record_steer` then refuses it (`STEER_CONFLICT_ERROR`) instead of the first
        steer's 202 coming back for it."""
        return f"steer:{self.steer_id}:{sha256(self.body.encode()).hexdigest()[:16]}"


class SteerStarted(_Payload):
    """`steer`'s result, and `POST /threads/{id}/steers`' 202 body. `escalation_id` and
    `draft_rev` are set when the item is being rewritten, as that rev."""

    steer_id: UUID
    escalation_id: UUID | None
    draft_rev: int | None


class UseAnswer(_Payload):
    """Update `use_answer`: draft the item's next rev from a member's answer to another
    item on the same card (`from_escalation_id`, which they approved). The API has
    checked that the member may act on both."""

    escalation_id: UUID
    from_escalation_id: UUID


class AnswerUsed(_Payload):
    """`use_answer`'s result: the rev being drafted."""

    escalation_id: UUID
    draft_rev: int


class ThreadProgress(_Payload):
    """Query `state`."""

    thread_id: UUID
    state: ThreadState
    side_to_move: Side | None
    requester_turns: int
    responder_turns: int
    open_escalation_ids: list[UUID]
    # Tool results each side's next turn must open with.
    owed: dict[Side, list[OwedResult]]
    last_stop: TurnStop | None


__all__ = [
    "BUDGET_EXHAUSTED_ERROR",
    "BUDGET_FAILURES",
    "CARD_NOT_PENDING_ERROR",
    "CONVERSATION_IDLE",
    "CONVERSATION_KIND",
    "CONVERSATION_TURNS_PER_RUN",
    "DRAFT_TEXT_MAX_CHARS",
    "ESCALATION_NOT_OPEN_ERROR",
    "INVALID_RESOLUTION_ERROR",
    "LOOK_EVERY",
    "LOOK_LATEST",
    "LOOK_LULL",
    "NOT_FOUND_ERROR",
    "NOT_IN_ORG_ERROR",
    "REQUEST_KIND",
    "STEER_BODY_MAX_CHARS",
    "STEER_CONFLICT_ERROR",
    "STEER_RUNNING_ERROR",
    "THREAD_CLOSED_ERROR",
    "THREAD_KIND",
    "AnswerUsed",
    "BudgetExhaustedDetails",
    "CardResolved",
    "ConversationCarry",
    "ConversationInput",
    "ConversationPending",
    "ConversationResult",
    "EscalationResolved",
    "MemberMessage",
    "NetworkNews",
    "NetworkRequestInput",
    "NetworkRequestResult",
    "NetworkUpdate",
    "NetworkUpdateKind",
    "Reassigned",
    "Reassignment",
    "ReopenInput",
    "RequestProgress",
    "SteerRequest",
    "SteerStarted",
    "ThreadCarry",
    "ThreadChanged",
    "ThreadInput",
    "ThreadProgress",
    "ThreadProgressRef",
    "ThreadResult",
    "UseAnswer",
    "conversation_workflow_id",
    "request_workflow_id",
    "thread_workflow_id",
    "update_failure",
]
