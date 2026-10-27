"""Domain errors. API routes map them to typed `ApiProblem` bodies; activities let the
non-retryable ones fail fast (Temporal matches them by class name, so don't rename
them casually; see `harmony.errors`)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from harmony.errors import HarmonyError


class CardNotPending(HarmonyError):
    """The card was already approved, rejected or superseded (409)."""

    def __init__(self, card_id: UUID, status: str) -> None:
        super().__init__(f"card {card_id} is {status}, not pending")
        self.card_id = card_id
        self.status = status


class EscalationNotOpen(HarmonyError):
    """Someone resolved the item first, or its thread ended (409). Carries who resolved
    it and when, for "Resolved by Helen 10 seconds ago" (§10 Concurrency)."""

    def __init__(
        self,
        escalation_id: UUID,
        status: str,
        *,
        resolved_by_member_id: UUID | None = None,
        resolved_at: datetime | None = None,
    ) -> None:
        super().__init__(f"escalation {escalation_id} is {status}, not open")
        self.escalation_id = escalation_id
        self.status = status
        self.resolved_by_member_id = resolved_by_member_id
        self.resolved_at = resolved_at


class ConsultNotOpen(HarmonyError):
    """The consult was already declined (or released), so it can't move that way."""

    def __init__(self, consult_id: UUID, status: str) -> None:
        super().__init__(f"consult {consult_id} is {status}")
        self.consult_id = consult_id
        self.status = status


class NotInOrg(HarmonyError):
    """A member tried to act on another org's escalation (403)."""

    def __init__(self, member_id: UUID, org_id: UUID) -> None:
        super().__init__(f"member {member_id} isn't in org {org_id}")
        self.member_id = member_id
        self.org_id = org_id


class NotAdmin(HarmonyError):
    """Only an org admin may change Harmony settings (403)."""


class InvalidRoute(HarmonyError):
    """A routing decision named nobody valid; callers fall back to the default
    assignee."""


class SteerConflict(HarmonyError):
    """A steer id already stored with different content: one client reused its key for
    different text (422). The class name is `payloads.STEER_CONFLICT_ERROR`."""

    def __init__(self, steer_id: UUID) -> None:
        super().__init__(f"steer {steer_id} was already recorded with different content")
        self.steer_id = steer_id


class ReopenConflict(HarmonyError):
    """A reopened run found its thread not ready for it: live, or ended at another run,
    or still wrapping up (REOPEN.md §4.4). The reopen route's checks make it a bug."""

    def __init__(self, thread_id: UUID, reason: str) -> None:
        super().__init__(f"thread {thread_id} can't be reopened: {reason}")
        self.thread_id = thread_id


class SettingsConflict(HarmonyError):
    """A settings write lost to another (its version is stale), or would add a trusted
    organization twice (409)."""


__all__ = [
    "CardNotPending",
    "ConsultNotOpen",
    "EscalationNotOpen",
    "InvalidRoute",
    "NotAdmin",
    "NotInOrg",
    "ReopenConflict",
    "SettingsConflict",
    "SteerConflict",
]
