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

from datetime import datetime, timedelta
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from temporalio.exceptions import ActivityError, ApplicationError

from harmony.agents.models import EscalationAction, RequestStatus
from harmony.agents.runtime import OwedResult, TurnStop
from harmony.db.models.choices import CardStatus, Side, ThreadState
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

# A conversation workflow continues as new after this many turns, and exits after
# this long without a message, an update or a pending card.
CONVERSATION_TURNS_PER_RUN: Final = 50
CONVERSATION_IDLE: Final = timedelta(hours=1)


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


class ConversationCarry(_Payload):
    """What a conversation run hands to the next one on continue-as-new."""

    queued_messages: list[MemberMessage] = []
    network_updates: list[NetworkUpdate] = []
    # Internal turns taken so far (the cassette turn number continues from here).
    turns: int = Field(default=0, ge=0)
    # Requests whose `closed` update was taken: a repeat (a retried signal) is dropped.
    closed_request_ids: list[UUID] = []
    # `closed` updates a turn took but never stored (`ConversationWorkflow._undelivered`).
    undelivered_updates: list[NetworkUpdate] = []
    # The last turn failed, and no turn has run since.
    turn_failed: bool = False


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
    # The last turn failed (Claude couldn't reply), and no turn has run since.
    turn_failed: bool = False


# --- NetworkRequestWorkflow ----------------------------------------------------------


class NetworkRequestInput(_Payload):
    world_id: UUID
    request_id: UUID
    requester_org_id: UUID
    conversation_id: UUID | None
    recipient_org_ids: list[UUID] = Field(min_length=1)
    max_active_threads: int = Field(default=8, ge=1)


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


class ThreadInput(_Payload):
    world_id: UUID
    thread_id: UUID
    request_id: UUID
    requester_org_id: UUID
    responder_org_id: UUID
    # The thread's cassette key (`agents.keys.thread_key`), fixed when it opens.
    cassette_thread: str
    max_turns: int = Field(default=16, ge=1)
    # The thread ends `expired` here, whatever it waits on (a person included), so it
    # always writes its own terminal state inside the 24 h execution timeout.
    deadline_at: datetime | None = None


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


class Redraft(_Payload):
    """Update `redraft`: "Ask Claude to redraft" an open consult escalation's answer
    with the member's instruction (a new `draft_rev`; the escalation stays open)."""

    escalation_id: UUID
    instruction: str = Field(min_length=1, max_length=4000)
    by_member_id: UUID


class RedraftStarted(_Payload):
    escalation_id: UUID
    # The rev the answerer is drafting; it appears as an `escalation_draft` snapshot.
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
    "CARD_NOT_PENDING_ERROR",
    "CONVERSATION_IDLE",
    "CONVERSATION_KIND",
    "CONVERSATION_TURNS_PER_RUN",
    "ESCALATION_NOT_OPEN_ERROR",
    "INVALID_RESOLUTION_ERROR",
    "NOT_FOUND_ERROR",
    "NOT_IN_ORG_ERROR",
    "REQUEST_KIND",
    "THREAD_KIND",
    "CardResolved",
    "ConversationCarry",
    "ConversationInput",
    "ConversationPending",
    "ConversationResult",
    "EscalationResolved",
    "MemberMessage",
    "NetworkRequestInput",
    "NetworkRequestResult",
    "NetworkUpdate",
    "NetworkUpdateKind",
    "Reassigned",
    "Reassignment",
    "Redraft",
    "RedraftStarted",
    "RequestProgress",
    "ThreadChanged",
    "ThreadInput",
    "ThreadProgress",
    "ThreadProgressRef",
    "ThreadResult",
    "conversation_workflow_id",
    "request_workflow_id",
    "thread_workflow_id",
    "update_failure",
]
