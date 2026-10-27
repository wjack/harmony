"""The release decision: whether a consult answer goes back to the representative
without a person (REVISION_UNSTRUCTURED.md §4.4).

| Mode | Released without a person when |
|---|---|
| *Ask* | never: a teammate reviews every answer |
| *Auto* (default) | the answerer says no human is needed and the sharing check passes |
| *Skip* | the answerer says no human is needed (no sharing check) |

A draft from a member's answer to an earlier question (`reused`) goes to a counterparty
that answer wasn't written for, so it is checked in every mode: the check's verdict
counts in *Skip* too.

The mode and the sharing descriptions are per counterparty: a trusted organization's
own mode, when set, replaces the org default, and its extra description is added to
the default one (§3). Policy is read live at each decision, so callers pass the rows
they just loaded.

Pure functions: no I/O, deterministic.

Owner: R-agents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from harmony.db.models import Consult, RepPolicy, TrustedOrganization
from harmony.db.models.choices import Autonomy

if TYPE_CHECKING:
    from harmony.agents.consult import ConsultDraft, SharingCheck

# Why an answer waits for a person. `no_draft`: the answerer produced no usable draft.
NeedsHumanReason = Literal["needs_human", "sharing_check", "ask_mode", "no_draft"]

_NO_DRAFT = "Claude couldn't draft an answer."
_ASK_MODE = "A teammate approves every answer in Ask mode."


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Release(_Model):
    kind: Literal["release"] = "release"
    answer: str


class NeedsHuman(_Model):
    kind: Literal["needs_human"] = "needs_human"
    reason: NeedsHumanReason
    # "Why this needs you", as the reviewer reads it.
    detail: str


ReleaseDecision = Annotated[Release | NeedsHuman, Field(discriminator="kind")]


def mode_for(policy: RepPolicy, trusted: TrustedOrganization | None) -> Autonomy:
    """The org's mode toward one counterparty: its trusted-organization row's, else the
    default."""
    if trusted is not None and trusted.autonomy is not None:
        return trusted.autonomy
    return policy.autonomy


def needs_sharing_check(mode: Autonomy, draft: ConsultDraft, *, reused: bool = False) -> bool:
    """Whether the decision needs the sharing check: *Auto* uses it, and so does a
    `reused` draft in any mode, but only on a draft the answerer would release."""
    return (mode == "auto" or reused) and not draft.needs_human


def decide(
    mode: Autonomy, draft: ConsultDraft, sharing: SharingCheck | None, *, reused: bool = False
) -> Release | NeedsHuman:
    """Apply the table above. `sharing` is the check's verdict, required when
    `needs_sharing_check`. A check that couldn't run counts as failed: the caller stores
    `SharingCheck(within_sharing=False, reason=...)` and passes it, so `why_needed`
    reads the same reason back."""
    if draft.needs_human:
        return NeedsHuman(reason="needs_human", detail=_answerer_reason(draft.needs_human_reason))
    if mode == "auto" or reused:
        if sharing is None:
            raise ValueError("Auto mode and a reused answer need the sharing check's verdict")
        if not sharing.within_sharing:
            return NeedsHuman(reason="sharing_check", detail=sharing.reason)
    if mode == "ask":
        return NeedsHuman(reason="ask_mode", detail=_ASK_MODE)
    return Release(answer=draft.answer)


def why_needed(consult: Consult, held_reason: str | None = None) -> str:
    """ "Why this needs you" for a consult waiting on a person: the current draft's own
    reason when it has one (`held_reason`, a rev drafted from a member's earlier answer),
    else read back from the consult's row in the order `decide` applies."""
    if held_reason:
        return held_reason
    if consult.answer is None:
        return _NO_DRAFT
    if consult.needs_human:
        return _answerer_reason(consult.needs_human_reason)
    check = consult.sharing_check
    if check is not None and check.get("within_sharing") is False:
        return str(check.get("reason", ""))
    return _ASK_MODE


def _answerer_reason(reason: str | None) -> str:
    return reason.strip() if reason and reason.strip() else "Claude asked for a person."


__all__ = [
    "NeedsHuman",
    "NeedsHumanReason",
    "Release",
    "ReleaseDecision",
    "decide",
    "mode_for",
    "needs_sharing_check",
    "why_needed",
]
