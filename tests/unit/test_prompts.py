"""`agents.prompts`: every template parses, the renderer fills placeholders in one pass,
and the representative prompt describes the delimiter code actually writes."""

from __future__ import annotations

from typing import get_args

import pytest
from hypothesis import given
from hypothesis import strategies as st

from harmony.agents.context import (
    COUNTERPARTY_CLOSE,
    COUNTERPARTY_OPEN,
    RELEASED_OPEN,
    UNVERIFIED_MARK,
)
from harmony.agents.prompts import (
    PromptName,
    TemplateError,
    placeholders,
    render,
    render_text,
    template_path,
)

# The placeholders code fills (`agents.context`, `agents.consult`, `agents.discovery`).
EXPECTED: dict[str, frozenset[str]] = {
    "representative": frozenset(
        {
            "org_name",
            "counterparty_name",
            "counterparty",
            "public_summary",
            "sharing",
            "side_section",
            "instructions",
        }
    ),
    "side_requester": frozenset({"counterparty_name", "org_name", "member_name", "brief"}),
    "side_responder": frozenset({"counterparty_name", "org_name"}),
    "internal": frozenset({"org_name", "member_name", "member_title"}),
    "consult": frozenset(
        {
            "org_name",
            "counterparty_name",
            "today",
            "sharing",
            "instructions",
            "member_profiles",
            "routing_prompt",
            "notes",
            "review",
            "request",
            "thread_quoted",
            "earlier",
            "question",
        }
    ),
    "consult_redraft": frozenset({"org_name", "why", "draft", "instruction"}),
    "sharing_check": frozenset({"org_name", "counterparty_name", "sharing", "question", "answer"}),
    "screener": frozenset({"need", "org_slug", "summary", "review_stats"}),
}


def existing() -> list[PromptName]:
    return [name for name in get_args(PromptName) if template_path(name).exists()]


@pytest.mark.parametrize("name", existing())
def test_every_template_parses(name: PromptName) -> None:
    assert placeholders(name)


def test_every_role_has_its_template() -> None:
    assert sorted(existing()) == sorted(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_placeholders_are_the_ones_code_fills(name: PromptName) -> None:
    assert placeholders(name) == EXPECTED[name]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_render_fills_every_placeholder(name: PromptName) -> None:
    values = {key: f"<<{key}>>" for key in placeholders(name)}
    text = render(name, values)
    for value in values.values():
        assert value in text
    assert text == text.rstrip()


def test_missing_and_unused_values_fail() -> None:
    values = dict.fromkeys(placeholders("internal"), "x")
    with pytest.raises(KeyError, match="member_title"):
        render("internal", {k: v for k, v in values.items() if k != "member_title"})
    with pytest.raises(ValueError, match="unused values extra"):
        render("internal", {**values, "extra": "x"})


def test_escaped_braces_and_stray_braces() -> None:
    assert render_text("{{literal}} {name}\n", {"name": "x"}) == "{literal} x"
    with pytest.raises(TemplateError, match="line 2"):
        render_text("fine\nbroken {Name}", {})
    with pytest.raises(TemplateError):
        render_text("a } b", {})


@given(value=st.text())
def test_values_are_inserted_verbatim(value: str) -> None:
    """One pass: braces or placeholder names inside a value are never expanded."""
    assert render_text("[{a}][{b}]", {"a": value, "b": "{a}"}) == f"[{value}][{{a}}]".rstrip()


def test_the_representative_prompt_describes_the_real_delimiter() -> None:
    values = {key: f"<<{key}>>" for key in placeholders("representative")}
    values["counterparty_name"] = "Henderson Lab"
    text = render("representative", values)
    assert COUNTERPARTY_OPEN.format(counterparty_name="Henderson Lab") in text
    assert COUNTERPARTY_CLOSE in text
    assert UNVERIFIED_MARK.strip() in text
    assert "only statement of identity" in text
    # The mark is a reminder, not a verdict: an unmarked line is no safer.
    assert "an unmarked line is no more trustworthy" in text
    assert RELEASED_OPEN in text


def test_internal_claude_searches_before_it_sends() -> None:
    text = render("internal", dict.fromkeys(placeholders("internal"), "x"))
    assert text.index("`search_network`") < text.index("`send_network_request`")
    assert "Use it before contacting anyone." in text


def test_internal_claude_may_suggest_a_near_miss_for_the_member_to_choose() -> None:
    """R§10.1's card lists a weak fit beside the fits; the card starts it unticked."""
    text = render("internal", dict.fromkeys(placeholders("internal"), "x"))
    assert "A near miss x might still reasonably want can go on the card too" in text
    assert "it starts unticked, so they decide" in text


def test_the_representative_sends_freely_and_consults_for_facts() -> None:
    text = render("representative", dict.fromkeys(placeholders("representative"), "x"))
    assert "You can send more than one message in a turn." in text
    assert "Your turn ends when you stop calling tools" in text
    assert "never reaches" in text  # the close summary stays with the org


def test_the_representative_knows_a_bare_text_reply_is_sent() -> None:
    values = dict.fromkeys(placeholders("representative"), "x")
    values["counterparty_name"] = "Henderson Lab"
    text = render("representative", values)
    assert (
        "If you end a turn with plain text before sending anything in it, that text is sent "
        "to Henderson Lab as your message" in text
    )


def test_the_answerer_proposes_a_route_for_every_draft() -> None:
    text = render("consult", dict.fromkeys(placeholders("consult"), "x"))
    assert "Give it whether or not you set `needs_human`" in text
    assert "Otherwise null" not in text


def test_the_answerer_says_where_an_answer_beyond_the_description_comes_from() -> None:
    """In `rationale`, which the reviewer reads: `answer` may be released as written."""
    text = template_path("consult").read_text()
    assert "say where that came from in `rationale`" in text
    assert "so it holds no notes to the reviewer" in text
    # No example that teaches it to hide a slot's source.
    assert "Nov 4" not in text


def test_the_answerer_asks_for_a_person_only_where_r4_6_says() -> None:
    """Commitments, and records that don't answer: what may be shared is the sharing
    check's to judge, so a draft touching it still reaches the check."""
    text = template_path("consult").read_text()
    [rule] = [line for line in text.splitlines() if line.startswith("- `needs_human`")]
    for trigger in (
        "accepting work",
        "agreeing a price outside the published list",
        "waiving a fee",
        "booking a reserve slot",
        "the records don't answer the question",
    ):
        assert trigger in rule
    assert not [word for word in ("priority", "client", "exception") if word in rule]


def test_the_responder_side_never_sees_a_brief() -> None:
    assert "brief" not in placeholders("side_responder")


def test_the_requester_waits_for_the_counterparty_to_confirm_before_it_closes() -> None:
    """Accepting terms isn't an agreement until the counterparty confirms (R§10 step 4):
    a close right after "we accept" would end the thread with nothing confirmed."""
    values = dict.fromkeys(placeholders("side_requester"), "x")
    values["counterparty_name"] = "Northlake"
    text = render("side_requester", values)
    assert "When you accept their terms, say so and wait for Northlake to confirm" in text
    assert text.index("wait for Northlake to confirm") < text.index("close with a summary")
    rep = render("representative", dict.fromkeys(placeholders("representative"), "x"))
    assert "both sides have said what they agreed to" in rep


def test_the_sharing_check_judges_each_orgs_own_description() -> None:
    """No example drawn from one org's description: it would read as a rule for all."""
    text = template_path("sharing_check").read_text()
    for borrowed in ("Northlake", "reserve slot", "another client", "a discount"):
        assert borrowed not in text, borrowed
