"""Value types shared by the agent layer, the domain services, the workflows and the API.

Conventions:
- References that a *model* writes are slugs or the IDs it was shown; references that
  *code* or the API writes are UUIDs.
- Every model here is frozen and forbids unknown fields, so a typo fails at the
  boundary instead of being stored. They travel through Temporal with the Pydantic data
  converter, through the API as OpenAPI schemas, and into jsonb columns via
  `model_dump(mode="json")`.
- What agents say to each other is prose: prices, terms and agreement are messages, and
  nothing here gives them structure (REVISION_UNSTRUCTURED.md §1).

Statuses that the database keeps as free text (`network_requests.status`,
`escalations.status`) are fixed here as Literals; `db/models/choices.py` holds the ones
the database enforces.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Final, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harmony.auth.views import OrgRef
from harmony.db.models.choices import CardRejectedWith, CardStatus, Fit

__all__ = [
    "BRIEF_MAX_CHARS",
    "BRIEF_TITLE_CHARS",
    "ActivityLine",
    "Ask",
    "AssistantTextItem",
    "Brief",
    "CardDecision",
    "CardPayload",
    "CardRecipient",
    "CardResolution",
    "ChatItem",
    "ChatItemType",
    "ContextPart",
    "ContextPartKind",
    "DeliveryState",
    "EscalationAction",
    "EscalationResolution",
    "EscalationStatus",
    "InlineQuestionItem",
    "MemberRef",
    "OutreachCardItem",
    "OutreachCardPayload",
    "OutreachEdits",
    "OutreachRecipient",
    "ParticipantStatus",
    "QuestionAsker",
    "QuestionDelivery",
    "RequestStatus",
    "RequestStatusItem",
    "Route",
    "RouteAssignee",
    "ScreenedCounts",
    "ThinkingSummaryItem",
    "ToolActivityItem",
    "ToolActivityStatus",
    "UserTextItem",
    "brief_title",
    "first_line",
]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


BRIEF_MAX_CHARS: Final = 8000
# The brief internal Claude writes and the member approves on the outreach card: the
# requester representative's mandate, in prose (§7). Approving it is what releases it.
Brief = Annotated[str, Field(min_length=1, max_length=BRIEF_MAX_CHARS)]
# What one recipient is asked, in prose, as the member may edit it on the card: only our
# representative in that recipient's conversation sees it (3.13 E2).
Ask = Annotated[str, Field(min_length=1, max_length=BRIEF_MAX_CHARS)]
BRIEF_TITLE_CHARS: Final = 80
# How many of a request's conversations run at once (`NetworkRequestInput`).
MAX_ACTIVE_THREADS: Final = 8


def first_line(text: str, limit: int) -> str:
    """The first non-blank line of `text`, cut to `limit` characters with an ellipsis."""
    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if len(line) <= limit:
        return line
    return line[: limit - 1].rstrip() + "…"


def brief_title(brief: str) -> str:
    """How views name a request: the brief's first line. Derived, never stored, so the
    brief stays the one source."""
    return first_line(brief, BRIEF_TITLE_CHARS)


# `network_requests.status`: `active` while any thread is live; `closed` once every
# thread has ended (closed or expired).
RequestStatus = Literal["active", "closed"]
# `escalations.status`: `open` until a member resolves it (any action) or code cancels
# it (its thread ended first).
EscalationStatus = Literal["open", "resolved", "cancelled"]
# A recipient's status in the network view and the request status card (§9):
# - `screened`: on the request, its thread not open yet;
# - `messaged`: its thread is open and it hasn't written yet;
# - `replied`: it has written;
# - `needs_human`: an escalation of the viewing org's is open on its thread (never the
#   other side's: domain/requests.py);
# - `closed`: its thread ended (closed, or expired).
ParticipantStatus = Literal["screened", "messaged", "replied", "needs_human", "closed"]


# --- People -------------------------------------------------------------------------


class MemberRef(_Model):
    """A member as other views show them (assignees, authors, resolvers)."""

    id: UUID
    slug: str
    name: str


# --- Consults and the inbox (§4, §6) -------------------------------------------------


class RouteAssignee(_Model):
    member: str = Field(description="A member slug.")
    # One short clause addressed to them: "you run XRD and SEM".
    why: str


class Route(_Model):
    """Who should review, each with why, as the answerer proposes it. Code validates
    the slugs against the org's members and falls back to the default assignee (§6)."""

    assignees: list[RouteAssignee]

    @model_validator(mode="before")
    @classmethod
    def _shared_reason(cls, data: object) -> object:
        """Drafts and cassettes from before per-assignee whys stored member slugs and
        one `reason`: each slug takes that reason as its why."""
        if not isinstance(data, dict):
            return data
        fields = cast(dict[str, Any], data)
        if "reason" not in fields:
            return fields
        slugs = cast(list[Any], fields.get("assignees", []))
        return {"assignees": [{"member": slug, "why": fields["reason"]} for slug in slugs]}


class ActivityLine(_Model):
    """One thing an agent did with a tool, as a line the reviewer reads: what internal
    Claude looked at while answering a consult (§9), from its tool calls."""

    tool: str
    label: str
    detail: str | None = None


# What a representative's frozen system prompt holds (§2), as the context inspector
# ("What our representative saw") lists it: `sharing` appears once for the org's
# default description and again for a trusted organization's extra one; `ask` is what
# the requester asks this one counterparty.
ContextPartKind = Literal[
    "public_summary", "instructions", "sharing", "brief", "ask", "counterparty"
]


class ContextPart(_Model):
    """One part of a representative's frozen system prompt, read back from the stored
    bytes (`rep_sessions.system_text`), so it is exactly what the model saw."""

    kind: ContextPartKind
    label: str
    text: str


# - `approve`: "Approve & send": release the draft answer (consult) or send the held
#   message (`outbound_review`), as written or replaced by `text` ("Edit"). Approval
#   releases it to this one thread only (§4.5).
# - `decline`: nothing is released or sent; the representative is told its side
#   declined, with `text` as the reason it may give.
# - `take_over`: the member writes to the counterparty directly (`text`, provenance
#   `human`); the escalation closes.
# A member's Edit (a rewrite of the draft) is the Update `steer`, and "Reassign" is its
# own Update (`Reassignment`).
EscalationAction = Literal["approve", "decline", "take_over"]


class EscalationResolution(_Model):
    """A member's resolution of an inbox item: the `resolve_escalation` Update's
    argument and the `escalations.resolution` column.

    The API names the acting member and their org here, because a Temporal Update
    validator can't read the database.
    """

    escalation_id: UUID
    action: EscalationAction
    text: str | None = None
    # The draft the member approved or edited; a newer draft doesn't invalidate the
    # resolution.
    draft_rev: int | None = None
    member_id: UUID
    org_id: UUID


# --- Cards (§5, §12) -----------------------------------------------------------------


class ScreenedCounts(_Model):
    """ "Screened 34 orgs: 3 strong, 4 possible"."""

    total: int = Field(ge=0)
    strong: int = Field(ge=0)
    possible: int = Field(ge=0)
    no: int = Field(ge=0)
    unknown: int = Field(ge=0)


class OutreachRecipient(_Model):
    """A proposed recipient as stored in the card payload and, once sent, in
    `network_requests.recipients`. Cards from before asks, titles and suggestions
    stored none."""

    org_id: UUID
    fit: Fit | None
    reason: str
    # What to ask this recipient alone (3.13 E2).
    ask: str | None = None
    # Its conversation's subject, which both sides see.
    title: str | None = None
    # Internal Claude suggests sending to it; false lists it for the member's decision.
    suggested: bool | None = None


class OutreachCardPayload(_Model):
    """`cards.payload` for `send_network_request`: the proposed brief and recipients."""

    kind: Literal["outreach"] = "outreach"
    brief: Brief
    recipients: list[OutreachRecipient]
    screened: ScreenedCounts | None = None
    # The `search_network` calls the card's fit and counts came from.
    search_ids: list[UUID] = []
    # The request's name for the member; never shown to a recipient.
    title: str | None = None
    # A card raised in a chat that sends without review: who it goes to (3.10).
    auto_send_recipients: list[UUID] | None = None


# `cards.payload`: the outreach approval is the one kind of card (`cards.kind`).
CardPayload = OutreachCardPayload

CardDecision = Literal["approve", "reject"]


class OutreachEdits(_Model):
    """Approval edits on an outreach card: a subset of the proposed recipients (org
    ids), the brief as the member rewrote it, and asks they rewrote, by org id."""

    recipients: list[UUID] | None = None
    brief: Brief | None = None
    asks: dict[UUID, Ask] | None = None


class CardResolution(_Model):
    """The `resolve_card` Update's argument and the `cards.resolution` column.

    A member message typed while a card is pending rejects it with that text as
    `feedback` (Claude Code style).
    """

    card_id: UUID
    decision: CardDecision
    member_id: UUID
    edits: OutreachEdits | None = None
    feedback: str | None = None
    # Settled by the workflow for a chat that sends without review, not by a member
    # (`member_id` is the chat's owner).
    auto: bool = False
    # How a rejection came: the card's Decline, or a message typed while it was
    # pending. None on approvals, and on rows and histories from before it existed.
    rejected_with: CardRejectedWith | None = None


# --- Chat items: the chat view model (GET /api/conversations/{id}) -------------------

ChatItemType = Literal[
    "user_text",
    "assistant_text",
    "thinking_summary",
    "tool_activity",
    "outreach_card",
    "request_status",
    "inline_question",
]
ToolActivityStatus = Literal["running", "done", "error"]


class _ChatItem(_Model):
    # Stable within the conversation, so the client can key and patch items:
    # `msg:<chat_message_id>:<block>`, `card:<card_id>`, `req:<request_id>`,
    # `esc:<escalation_id>`.
    id: str
    at: datetime


class UserTextItem(_ChatItem):
    type: Literal["user_text"] = "user_text"
    text: str
    member_id: UUID | None


class AssistantTextItem(_ChatItem):
    type: Literal["assistant_text"] = "assistant_text"
    markdown: str
    turn_id: UUID | None


class ThinkingSummaryItem(_ChatItem):
    type: Literal["thinking_summary"] = "thinking_summary"
    text: str
    turn_id: UUID | None


class ToolActivityItem(_ChatItem):
    """ "Searched the Harmony network · 2 possible fits of 14", over what it searched for
    (UI_REFERENCE §4)."""

    type: Literal["tool_activity"] = "tool_activity"
    tool: str
    # Present tense while it runs ("Searching the Harmony network…"), past once done.
    label: str
    # What it worked on: the need or query searched for, the record read.
    detail: str | None
    status: ToolActivityStatus
    # What it found, once done ("2 possible fits of 14"); None while running.
    outcome: str | None
    turn_id: UUID | None


class CardRecipient(_Model):
    org: OrgRef
    fit: Fit | None
    reason: str
    ask: str | None = None
    title: str | None = None
    suggested: bool | None = None
    # In the request: every listed row while pending or once rejected; once approved,
    # the rows the request went to (`requests.chosen_recipients`).
    selected: bool = True


class OutreachCardItem(_ChatItem):
    """The `send_network_request` approval, rendered as the ask-user card."""

    type: Literal["outreach_card"] = "outreach_card"
    card_id: UUID
    status: CardStatus
    # The request's name for the member (`OutreachCardPayload.title`).
    title: str | None = None
    # Raised in a chat that sends without review: the workflow settles it, never a
    # member (`cards.auto`).
    auto: bool = False
    brief: Brief
    recipients: list[CardRecipient]
    screened: ScreenedCounts | None
    # Set once rejected with feedback / once approved.
    feedback: str | None
    # How a member turned it down: its Decline, or a message typed while it waited
    # (None unless a member rejected it).
    rejected_with: CardRejectedWith | None
    request_id: UUID | None
    version: int


class RequestStatusItem(_ChatItem):
    """The live per-recipient card (statuses, latest lines, this side's close summaries);
    the client reads `GET /api/requests/{id}`."""

    type: Literal["request_status"] = "request_status"
    request_id: UUID


class QuestionAsker(_Model):
    """Another conversation on the request whose representative asked the card's
    question, routed to the chat's owner ("How each asked")."""

    escalation_id: UUID
    thread_id: UUID
    from_org: OrgRef
    # Our representative's question in that conversation.
    question: str
    status: EscalationStatus


# Where a card's answer went in one conversation:
# - `as_written`: the asker the card shows, sent the member's words (or another asker a
#   member answered in its own conversation);
# - `fitted`: another asker, sent Claude's version fitted to its question, after its
#   org's checks;
# - `drafting`: that version is being written;
# - `held`: the asker waits for a person (`reason`): its version didn't pass;
# - `not_sent`: the member unticked the asker, it asked after the answer, or a member
#   declined it;
# - `ended`: its conversation ended before the answer reached it.
DeliveryState = Literal["as_written", "fitted", "drafting", "held", "not_sent", "ended"]


class QuestionDelivery(_Model):
    """One row under an answered card: what became of the answer in one conversation."""

    thread_id: UUID
    org: OrgRef
    state: DeliveryState
    # When it was sent or held.
    at: datetime | None
    # Why it waits for a person (`held` only).
    reason: str | None


class InlineQuestionItem(_ChatItem):
    """A requester-side consult routed to the chat's owner (a counterparty's question,
    or terms to agree to), resolved through the same endpoint as the inbox (§7). A
    resolved one carries how it was settled (`action`) and the words that went with it
    (`answer`): the member's own, else the draft rev they approved as written. An item
    cancelled because its thread ended stays, as no longer needed.

    A card groups the request's items that ask the same question and are routed to the
    chat's owner. The one shown (`escalation_id`, `question`, `from_org`) is the item a
    member answered, else the oldest open one; the rest are `askers`. The answer goes to
    the one shown and the askers the member keeps ticked, never to another conversation.
    Answered, it lists one `deliveries` row per asker, and who answered (`answered_by`,
    `answered_at`)."""

    type: Literal["inline_question"] = "inline_question"
    escalation_id: UUID
    thread_id: UUID
    question: str
    from_org: OrgRef
    status: EscalationStatus
    action: EscalationAction | None
    answer: str | None
    askers: list[QuestionAsker] = []
    # The request's organizations: the member's answer may name none but the shown
    # asker's.
    recipients: list[OrgRef] = []
    deliveries: list[QuestionDelivery] = []
    answered_by: MemberRef | None
    answered_at: datetime | None


ChatItem = Annotated[
    UserTextItem
    | AssistantTextItem
    | ThinkingSummaryItem
    | ToolActivityItem
    | OutreachCardItem
    | RequestStatusItem
    | InlineQuestionItem,
    Field(discriminator="type"),
]
