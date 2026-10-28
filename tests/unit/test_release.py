"""The release decision by mode (REVISION_UNSTRUCTURED.md §4.4, §11): *Ask* releases
nothing, *Auto* only a draft the answerer would release that passes the sharing check,
*Skip* any draft the answerer would release; a draft from a member's earlier answer is
checked in every mode; a trusted organization's mode and sharing description apply to
that counterparty only. Demo controls' Skip approvals asks nobody in any mode."""

from __future__ import annotations

from typing import Final, get_args

import pytest
from hypothesis import given
from hypothesis import strategies as st

from harmony.agents import context, release
from harmony.agents.consult import ConsultDraft, SharingCheck
from harmony.agents.release import Decline, NeedsHuman, Release
from harmony.db.models import Consult, RepPolicy, TrustedOrganization
from harmony.db.models.choices import Autonomy

MODES: Final = get_args(Autonomy)
DEFAULT_SHARING: Final = "Our external price list and the public instrument calendar."
EXTRA_SHARING: Final = "Also share with them: reserve-slot availability."

ANSWERS = st.text(min_size=1, max_size=200)
REASONS = st.one_of(st.none(), st.text(max_size=80))
CHECKS = st.builds(SharingCheck, within_sharing=st.booleans(), reason=st.text(max_size=80))


def draft(
    answer: str = "Powder XRD is $45 per sample.",
    *,
    needs_human: bool = False,
    reason: str | None = None,
) -> ConsultDraft:
    return ConsultDraft(
        answer=answer,
        needs_human=needs_human,
        needs_human_reason=reason,
        route=None,
        rationale="",
    )


def policy(mode: Autonomy = "auto", sharing: str = DEFAULT_SHARING) -> RepPolicy:
    return RepPolicy(autonomy=mode, sharing_description=sharing)


def trusted(mode: Autonomy | None = None, sharing: str = "") -> TrustedOrganization:
    return TrustedOrganization(autonomy=mode, sharing_description=sharing)


# --- The table ---------------------------------------------------------------------------


@given(answer=ANSWERS, needs_human=st.booleans(), reason=REASONS, check=st.none() | CHECKS)
def test_ask_releases_nothing(
    answer: str, needs_human: bool, reason: str | None, check: SharingCheck | None
) -> None:
    decision = release.decide("ask", draft(answer, needs_human=needs_human, reason=reason), check)
    assert isinstance(decision, NeedsHuman)
    assert decision.reason == ("needs_human" if needs_human else "ask_mode")


@given(answer=ANSWERS, reason=REASONS, check=CHECKS)
def test_auto_releases_only_a_checked_draft_the_answerer_would_release(
    answer: str, reason: str | None, check: SharingCheck
) -> None:
    released = release.decide("auto", draft(answer), check)
    if check.within_sharing:
        assert released == Release(answer=answer)
    else:
        assert released == NeedsHuman(reason="sharing_check", detail=check.reason)
    held = release.decide("auto", draft(answer, needs_human=True, reason=reason), check)
    assert isinstance(held, NeedsHuman)
    assert held.reason == "needs_human"


@given(answer=ANSWERS, reason=REASONS, check=st.none() | CHECKS)
def test_skip_releases_what_the_answerer_would_and_nothing_else(
    answer: str, reason: str | None, check: SharingCheck | None
) -> None:
    assert release.decide("skip", draft(answer), check) == Release(answer=answer)
    held = release.decide("skip", draft(answer, needs_human=True, reason=reason), check)
    assert isinstance(held, NeedsHuman)
    assert held.reason == "needs_human"


@given(
    mode=st.sampled_from(MODES),
    answer=ANSWERS,
    needs_human=st.booleans(),
    reused=st.booleans(),
    check=CHECKS,
)
def test_skip_approvals_asks_nobody_and_checks_every_draft(
    mode: Autonomy, answer: str, needs_human: bool, reused: bool, check: SharingCheck
) -> None:
    """Demo controls' Skip approvals skips the people, never the check: a draft that
    passes goes out even when the answerer flagged it, and one that fails is declined."""
    proposed = draft(answer, needs_human=needs_human)
    assert release.needs_sharing_check(mode, proposed, reused=reused, skip_people=True)
    decided = release.decide(mode, proposed, check, reused=reused, skip_people=True)
    if check.within_sharing:
        assert decided == Release(answer=answer)
    else:
        assert decided == Decline(detail=check.reason)


def test_skip_approvals_needs_the_verdict() -> None:
    with pytest.raises(ValueError, match="sharing check"):
        release.decide("skip", draft(), None, skip_people=True)


def test_auto_needs_the_verdict() -> None:
    with pytest.raises(ValueError, match="sharing check"):
        release.decide("auto", draft(), None)


@given(mode=st.sampled_from(MODES), needs_human=st.booleans(), reused=st.booleans())
def test_auto_or_a_reused_answer_runs_the_check_and_only_on_a_releasable_draft(
    mode: Autonomy, needs_human: bool, reused: bool
) -> None:
    """A draft from a member's earlier answer goes to a counterparty it wasn't written
    for, so it is checked in every mode, Skip included."""
    expected = (mode == "auto" or reused) and not needs_human
    proposed = draft(needs_human=needs_human)
    assert release.needs_sharing_check(mode, proposed, reused=reused) is expected


@pytest.mark.parametrize("mode", MODES)
def test_a_reused_answer_that_fails_the_check_goes_to_a_person_in_every_mode(
    mode: Autonomy,
) -> None:
    failed = SharingCheck(within_sharing=False, reason="It names another organization.")
    held = release.decide(mode, draft(), failed, reused=True)
    assert held == NeedsHuman(reason="sharing_check", detail="It names another organization.")
    with pytest.raises(ValueError, match="sharing check"):
        release.decide(mode, draft(), None, reused=True)
    passed = release.decide(
        mode, draft(), SharingCheck(within_sharing=True, reason=""), reused=True
    )
    assert isinstance(passed, NeedsHuman if mode == "ask" else Release)


def test_the_answerers_reason_is_what_the_reviewer_reads() -> None:
    decision = release.decide("skip", draft(needs_human=True, reason="  Helen's call. "), None)
    assert decision == NeedsHuman(reason="needs_human", detail="Helen's call.")
    blank = release.decide("skip", draft(needs_human=True, reason=" "), None)
    assert isinstance(blank, NeedsHuman)
    assert blank.detail == "Claude asked for a person."


# --- Per counterparty --------------------------------------------------------------------


@pytest.mark.parametrize("default", MODES)
@pytest.mark.parametrize("own", [None, *MODES])
def test_a_trusted_organizations_mode_replaces_the_default_for_it_only(
    default: Autonomy, own: Autonomy | None
) -> None:
    assert release.mode_for(policy(default), trusted(own)) == (own or default)
    # Any other counterparty has no row of its own: the default applies.
    assert release.mode_for(policy(default), None) == default


def test_a_trusted_organizations_sharing_is_added_for_it_only() -> None:
    """`context.sharing_parts` is what every prompt quotes: the default for any
    organization, then the trusted organization's own, for it."""

    def texts(org: RepPolicy, row: TrustedOrganization | None) -> list[str]:
        parts = context.sharing_parts(org, row, counterparty_name="Henderson Lab")
        return [part.text for part in context.context_parts("\n\n".join(parts))]

    org = policy(sharing=f"  {DEFAULT_SHARING}\n")
    assert texts(org, trusted(sharing=EXTRA_SHARING)) == [DEFAULT_SHARING, EXTRA_SHARING]
    assert texts(org, None) == [DEFAULT_SHARING]
    assert texts(org, trusted(sharing="  ")) == [DEFAULT_SHARING]
    assert texts(policy(sharing=""), trusted(sharing=EXTRA_SHARING)) == [EXTRA_SHARING]


# --- "Why this needs you" ----------------------------------------------------------------


def consult(**fields: object) -> Consult:
    return Consult(**{"answer": None, "needs_human": None, "sharing_check": None, **fields})


def test_why_needed_reads_back_the_reason_in_decision_order() -> None:
    assert release.why_needed(consult()) == "Claude couldn't draft an answer."
    assert (
        release.why_needed(
            consult(answer="x", needs_human=True, needs_human_reason="Helen's call.")
        )
        == "Helen's call."
    )
    failed = {"within_sharing": False, "reason": "Reserve slots aren't shared."}
    assert (
        release.why_needed(consult(answer="x", needs_human=False, sharing_check=failed))
        == "Reserve slots aren't shared."
    )
    assert release.why_needed(consult(answer="x", needs_human=False)).endswith("Ask mode.")
