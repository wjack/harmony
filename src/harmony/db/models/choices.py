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
    "CardKind",
    "CardStatus",
    "ChatRole",
    "ConsultStatus",
    "DirectoryVisibility",
    "EscalationKind",
    "Fit",
    "MemberRole",
    "ModelRole",
    "NoteAuthor",
    "Provenance",
    "ReleasedBy",
    "ReservationStatus",
    "ScopeKind",
    "Side",
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
ThreadMessageKind = Literal["message", "close"]
# Every message says whether an agent, an agent with a named human's approval, or a
# human who took over wrote it.
Provenance = Literal["agent", "agent_approved", "human"]

ConsultStatus = Literal["answering", "released", "needs_human", "declined"]
EscalationKind = Literal["consult", "outbound_review"]

Fit = Literal["strong", "possible", "no", "unknown"]

# Matches harmony.llm.ModelRole; duplicated so the schema has no llm import.
ModelRole = Literal[
    "internal", "representative", "answerer", "sharing_check", "synthesizer", "screener"
]

ChatRole = Literal["user", "assistant", "system"]
TranscriptRole = Literal["user", "assistant", "system"]

CardKind = Literal["outreach"]
CardStatus = Literal["pending", "approved", "rejected", "superseded"]

ReservationStatus = Literal["reserved", "settled", "released"]
