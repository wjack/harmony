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
    "side_requester": frozenset(
        {"counterparty_name", "org_name", "member_name", "approval", "brief", "ask"}
    ),
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
            "guidance",
            "review",
            "request",
            "thread_quoted",
            "earlier",
            "question",
        }
    ),
    "consult_redraft": frozenset(
        {"org_name", "member_first", "why", "draft_heading", "draft", "instruction"}
    ),
    "steer_guidance": frozenset(
        {
            "org_name",
            "counterparty_name",
            "side_line",
            "member_first",
            "guidance",
            "instruction",
            "waiting",
            "reopen",
        }
    ),
    "held_rewrite": frozenset(
        {
            "org_name",
            "counterparty_name",
            "side_line",
            "member_first",
            "thread_quoted",
            "guidance",
            "message",
            "instruction",
        }
    ),
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
    assert "`suggested` false" in text


def test_internal_claude_calls_them_harmony_network_requests() -> None:
    text = render("internal", dict.fromkeys(placeholders("internal"), "x"))
    assert 'call them Harmony Network Requests (just "requests" once that\'s clear)' in text


def test_internal_claude_writes_one_brief_and_an_ask_per_organization() -> None:
    """Each representative sees only its own conversation (3.13 E2): parts and limits
    go in each organization's ask, and a combined budget can't be split by anyone."""
    text = render("internal", dict.fromkeys(placeholders("internal"), "x"))
    assert "write only what holds for all of them" in text
    assert "Don't name the recipients in it." in text
    # Only our representative reads the brief: a recipient's own never sees it.
    assert "recipient's representative" not in text
    assert "give every limit per organization, in its ask, never as a combined figure" in text


def test_internal_claude_has_nothing_to_check_right_after_a_send() -> None:
    """Decision 7: the chat hears when something needs the member and when every
    conversation has ended, so Claude neither checks nor promises to check back."""
    text = render("internal", dict.fromkeys(placeholders("internal"), "x"))
    assert (
        "Right after a request goes out there's nothing to check, and no need to promise "
        "to check back: you're told here when something needs x and when every "
        "conversation has ended." in text
    )


def test_internal_claude_knows_either_send_mode_from_the_same_prompt() -> None:
    """Mode-neutral (3.10): the prompt's bytes never depend on the chat's setting."""
    text = render("internal", dict.fromkeys(placeholders("internal"), "x"))
    assert "Usually x approves first" in text
    assert "In a chat set to send without review it goes out at once" in text
    assert "The result says which, and what x changed." in text
    assert "Send one request per call: one need is one request" in text


def test_the_representative_sends_freely_and_consults_for_facts() -> None:
    text = render("representative", dict.fromkeys(placeholders("representative"), "x"))
    assert "You can send more than one message in a turn." in text
    assert "Your turn ends when you stop calling tools" in text
    assert "never reaches" in text  # the close summary stays with the org


def test_the_representative_waits_by_ending_its_turn_never_by_closing() -> None:
    """3.13 E1, E3: `close` ends the conversation, and a message is read exactly as
    written. Not "for good": a member can reopen it (decision 15)."""
    text = render("representative", dict.fromkeys(placeholders("representative"), "x"))
    assert "You never need `close` to wait." in text
    assert "`close` ends the conversation for both sides" in text
    assert "for good" not in text
    assert "They read the text exactly as written" in text
    assert "never send notes about the conversation" in text


def test_the_representative_consults_before_it_replies() -> None:
    """Decision 1: once a turn has sent, the move is the counterparty's, so a consult
    waits for the next turn (and `consult` refuses one after a send)."""
    values = dict.fromkeys(placeholders("representative"), "x")
    values["counterparty_name"] = "Northlake"
    text = render("representative", values)
    assert (
        "`consult` asks your own side. Consult before you reply: once you've sent a message "
        "this turn, it's Northlake's move, so ask at the start of your next turn, after "
        "theirs." in text
    )
    requester = render("side_requester", dict.fromkeys(placeholders("side_requester"), "x"))
    assert "consult before you reply, or at the start of your next turn" in requester
    assert "Once you've sent a message this turn, it's their move." in requester
    assert "report" not in requester


def test_the_representative_knows_plain_text_is_never_sent() -> None:
    """Section 5 bug 2: bare text is never sent (`runtime.BARE_TEXT_NOT_SENT`)."""
    values = dict.fromkeys(placeholders("representative"), "x")
    values["counterparty_name"] = "Henderson Lab"
    text = render("representative", values)
    assert "Only `send_message` reaches Henderson Lab" in text
    assert "fallback" not in text


def test_the_answerer_proposes_a_route_for_every_draft() -> None:
    text = render("consult", dict.fromkeys(placeholders("consult"), "x"))
    assert "Give it whether or not you set `needs_human`" in text
    assert "Otherwise null" not in text
    # 3.13 E12: a why per assignee, addressed to them, and the one-line question.
    assert "why it's them in one short clause addressed to that person" in text
    assert "- `short_question`:" in text
    redraft = render("consult_redraft", dict.fromkeys(placeholders("consult_redraft"), "x"))
    assert "with its own `route` and `short_question`" in redraft


def test_no_claude_reads_comments() -> None:
    """E3: the answerer's prompt has no notes section; comments are for teammates."""
    text = template_path("consult").read_text()
    assert "{notes}" not in text
    assert "Internal notes" not in text
    assert "an internal record)" in text


def test_the_answerer_says_where_an_answer_beyond_the_description_comes_from() -> None:
    """In `rationale`, which the reviewer reads: `answer` may be released as written."""
    text = template_path("consult").read_text()
    assert "say where that came from in `rationale`" in text
    assert "so it holds no notes to the reviewer" in text
    # No example that teaches it to hide a slot's source.
    assert "Nov 4" not in text


def test_the_answerer_asks_for_a_person_only_where_r4_6_says() -> None:
    """Commitments only: what may be shared is the sharing check's to judge, so a draft
    touching it still reaches the check, and a gap in the records goes in the answer
    (section 5 bug 3: Haiku asked for a person on every consult)."""
    text = template_path("consult").read_text()
    [rule] = [line for line in text.splitlines() if line.startswith("- `needs_human`")]
    for trigger in (
        "accepting work",
        "agreeing a price outside the published list",
        "waiving a fee",
        "booking a reserve slot",
        "A gap in the records isn't a decision",
    ):
        assert trigger in rule
    assert not [word for word in ("priority", "client", "exception") if word in rule]


def test_the_responder_side_never_sees_a_brief() -> None:
    assert "brief" not in placeholders("side_responder")
    assert "ask" not in placeholders("side_responder")


def test_the_requester_asks_only_for_its_own_part() -> None:
    """3.13 E2: the brief may cover other organizations; the ask is this one's."""
    values = dict.fromkeys(placeholders("side_requester"), "x")
    values["counterparty_name"] = "Northlake"
    text = render("side_requester", values)
    assert "Ask Northlake only for what the ask covers." in text
    assert "The brief may cover other organizations; never mention them or their parts." in text


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


def test_the_guidance_writer_never_sees_the_thread_or_what_the_org_shares() -> None:
    """EDIT.md §3.3: it writes from the members' words alone."""
    assert not placeholders("steer_guidance") & {
        "rep_parts",
        "thread_quoted",
        "sharing",
        "instructions",
        "draft",
        "message",
    }


def test_the_answerer_reads_guidance_that_never_widens_what_it_may_share() -> None:
    text = render("consult", {key: f"<<{key}>>" for key in placeholders("consult")})
    section = text[text.index("# What your teammates told the representative") :]
    assert "<<guidance>>" in section
    assert "never changes what" in section
    assert "notes" not in placeholders("consult")


def test_the_representative_prompt_describes_the_guidance_block() -> None:
    text = render("representative", {key: f"<<{key}>>" for key in placeholders("representative")})
    assert "`<guidance>` block that Harmony writes" in text
    after = text[text.index("<<instructions>>") :]
    assert "<guidance>" in after
