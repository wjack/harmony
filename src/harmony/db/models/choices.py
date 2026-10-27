"""The allowed values of the schema's enumerated text columns.

Each Literal is both the column's Python type and the source of its CHECK constraint
(`base.one_of`). Columns whose values the architecture has not fixed yet (request and
escalation status, notice kinds) stay free text, so P1 can settle them without a
migration. Adding a value here needs a migration that replaces the CHECK.
"""

from __future__ import annotations

from typing import Literal

from harmony.events.schemas import AccessReason, ScopeKind, WorldMode

__all__ = [
    "AccessReason",
    "Autonomy",
    "BudgetKind",
    "CardKind",
    "CardRejectedWith",
    "CardStatus",
    "ChatRole",
    "ConsultStatus",
    "DirectoryVisibility",
    "EndReason",
    "EscalationKind",
    "FailureReason",
    "Fit",
    "MemberRole",
    "ModelRole",
    "NoteAuthor",
    "Provenance",
    "ReleasedBy",
    "ReopenBlock",
    "ReservationStatus",
    "ScopeKind",
    "Side",
    "SteerDraftOutcome",
    "SteerFailure",
    "SteerGuidanceOutcome",
    "SteerKind",
    "SteerStatus",
    "ThreadMessageKind",
    "ThreadState",
    "TranscriptRole",
    "WorldMode",
    "WorldStatus",
]

WorldStatus = Literal["live", "retiring", "retired"]

# A thread has two sides; representatives, notices, consults and turns are per side.
Side = Literal["requester", "responder"]

MemberRole = Literal["admin", "member"]

# When a person gets involved: *Ask* reviews everything, *Auto* what needs a person or
# goes beyond what the org shares, *Skip* only what Claude says needs a person.
Autonomy = Literal["ask", "auto", "skip"]
DirectoryVisibility = Literal["public", "communities", "unlisted"]

# Who released a consult answer: code (`auto`) or a named member (`*_member_id`).
ReleasedBy = Literal["auto", "member"]
NoteAuthor = Literal["member", "claude"]

# ThreadWorkflow states; `closed` and `expired` are terminal.
ThreadState = Literal["active", "awaiting_principal", "closed", "expired"]
# Why a turn failed or a thread stopped (3.5); one enum for every surface. The budget
# reasons come from `BudgetKind` through `payloads.BUDGET_FAILURES`.
FailureReason = Literal[
    "session_budget", "daily_budget", "demo_budget", "paused", "model_error", "refused"
]
# Why a run of a thread ended (REOPEN.md §2.1): every surface shows "Closed" and this.
# Null while the thread is open, and on terminal rows written before it existed.
EndReason = Literal["closed", "quiet", "unsent", "turn_limit", "deadline", "stopped"]
# Why the viewer's side can't reopen a thread (REOPEN.md §3.2). `unfinished` comes
# only from the reopen route's 409, never from the thread read.
ReopenBlock = Literal["history", "unfinished", "not_asked", "limit", "refused", "they_closed"]
# Which hosted cap refused a call (`BudgetExhausted.kind`, 3.4).
BudgetKind = Literal["session", "client", "daily", "global", "paused", "provider"]
ThreadMessageKind = Literal["message", "close"]
# Every message says whether an agent, an agent with a named human's approval, or a
# human who took over wrote it.
Provenance = Literal["agent", "agent_approved", "human"]

# A member's instruction to its side's Claude (EDIT.md §2.1): an Edit, or Reopen's text.
SteerKind = Literal["edit", "reopen"]
SteerStatus = Literal["working", "done", "failed"]
SteerGuidanceOutcome = Literal["updated", "unchanged", "rejected", "failed"]
SteerDraftOutcome = Literal["rewritten", "settled", "failed"]
# Why a steer failed: a turn failure, the guidance check's rejections, the thread
# closing first, or (reopen steers only) `unchanged` and `reopen_failed`.
SteerFailure = Literal[
    FailureReason, "added_details", "invalid", "thread_closed", "unchanged", "reopen_failed"
]

ConsultStatus = Literal["answering", "released", "needs_human", "declined"]
EscalationKind = Literal["consult", "outbound_review"]

Fit = Literal["strong", "possible", "no", "unknown"]

# Matches harmony.llm.ModelRole; duplicated so the schema has no llm import.
ModelRole = Literal[
    "internal", "representative", "answerer", "sharing_check", "synthesizer", "screener", "steer"
]

ChatRole = Literal["user", "assistant", "system"]
TranscriptRole = Literal["user", "assistant", "system"]

CardKind = Literal["outreach"]
CardStatus = Literal["pending", "approved", "rejected", "superseded"]
# How a rejected card was rejected: the Decline step, or a member message typed while it
# waited.
CardRejectedWith = Literal["decline", "message"]

ReservationStatus = Literal["reserved", "settled", "released"]
