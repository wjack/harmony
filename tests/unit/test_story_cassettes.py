"""The demo's cassettes (tests/fixtures/cassettes/) against the contract: tool names and
input schemas, the answerer's, sharing check's and screener's output schemas, cassette
keys and turn shapes, and what they play: the suggested story
(scenarios/harmony-demo/story.yaml, REVISION_UNSTRUCTURED.md §10) and the adversary's
open request (threads.yaml)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from harmony.agents import keys
from harmony.agents.consult import ConsultDraft, DraftReady, SharingCheck, parse_answer
from harmony.agents.discovery import parse_verdict
from harmony.agents.models import brief_title
from harmony.agents.release import NeedsHuman, Release, decide, needs_sharing_check
from harmony.agents.tools.internal import (
    ANSWERER_TOOLS,
    INTERNAL_INPUTS,
    SendNetworkRequestInput,
)
from harmony.agents.tools.rep import REP_INPUTS
from harmony.llm.scripted import Cassette, CassetteEntry, ScriptedModel, load_cassettes
from harmony.seeds.ids import seed_id
from harmony.seeds.loader import Scenario, load_scenario
from harmony.seeds.schema import OrgSpec

CASSETTES = Path(__file__).parents[1] / "fixtures" / "cassettes"
SCENARIO = "harmony-demo"
CHAT = keys.chat_key("alex", 1)
NORTHLAKE = keys.thread_key("henderson", "northlake")
RIDGELINE = keys.thread_key("henderson", "ridgeline")
CALDER = keys.thread_key("henderson", "calder")
VANTAGE = keys.thread_key("vantage", "northlake")
STORY_FILES = ("suggested_chat", "suggested_screen", "suggested_threads", "adversary")
# Every cassette a demo world plays: the story's and the representative-turn tests'.
DEMO_FILES = (*(f"story_{name}" for name in STORY_FILES), "rep_turn")
ANSWERER_TOOL_NAMES = {str(tool["name"]) for tool in ANSWERER_TOOLS}


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    return load_scenario(SCENARIO)


@cache
def cassette(name: str) -> Cassette:
    raw = (CASSETTES / f"{name}.yaml").read_text(encoding="utf-8")
    return Cassette.model_validate(yaml.safe_load(raw))


def entries(*names: str) -> list[CassetteEntry]:
    return [e for name in names or DEMO_FILES for e in cassette(name).entries]


def find(role: str, org: str, thread: str, turn: int, step: int = 0) -> CassetteEntry | None:
    found = [
        e
        for e in entries()
        if (e.match.role, e.match.org, e.match.thread, e.match.turn, e.match.step)
        == (role, org, thread, turn, step)
    ]
    assert len(found) <= 1, (role, org, thread, turn, step)
    return found[0] if found else None


def entry(role: str, org: str, thread: str, turn: int, step: int = 0) -> CassetteEntry:
    found = find(role, org, thread, turn, step)
    assert found is not None, (role, org, thread, turn, step)
    return found


def tool_calls(e: CassetteEntry) -> list[tuple[str, dict[str, Any]]]:
    return [(b.name, dict(b.input)) for b in e.content if b.type == "tool_use"]


def text_of(e: CassetteEntry) -> str:
    return "".join(b.text for b in e.content if b.type == "text")


def message(e: CassetteEntry) -> dict[str, Any]:
    """The response a ScriptedModel plays for `e`, as the parsers read it."""
    stop = "tool_use" if tool_calls(e) else "end_turn"
    return {"content": [b.model_dump() for b in e.content], "stop_reason": stop}


def org(scenario: Scenario, slug: str) -> OrgSpec:
    return next(o for o in scenario.orgs if o.slug == slug)


def record_ref(slug: str, item: str) -> str:
    """How `search_org_knowledge` names a record, and `read_document` takes it."""
    return f"kn:{seed_id(SCENARIO, 'knowledge_item', f'{slug}/{item}')}"


def answerer_turn(org_slug: str, thread: str, turn: int) -> list[CassetteEntry]:
    """Draft rev 1's tool loop, step by step, through the draft."""
    steps: list[CassetteEntry] = []
    for step in range(keys.ANSWERER_STEPS):
        e = entry("answerer", org_slug, thread, turn, keys.answerer_step(1, step))
        steps.append(e)
        if not tool_calls(e):
            return steps
    pytest.fail(f"no draft in {org_slug}'s answerer turn {turn} on {thread}")


def draft(org_slug: str, thread: str, turn: int = 1) -> ConsultDraft:
    final = answerer_turn(org_slug, thread, turn)[-1]
    result = parse_answer(message(final), turn_id=uuid4(), step_idx=final.match.step)
    assert isinstance(result, DraftReady), result
    return result.draft


def sharing_check(org_slug: str, thread: str, turn: int = 1) -> SharingCheck:
    return SharingCheck.model_validate_json(text_of(entry("sharing_check", org_slug, thread, turn)))


def answerers() -> Iterator[tuple[str, str, int]]:
    """(org, thread, consult number) of every answerer turn the cassettes play."""
    seen = {
        (e.match.org, e.match.thread or "", e.match.turn)
        for e in entries()
        if e.match.role == "answerer"
    }
    yield from sorted(seen)


def moves(thread: str, *files: str) -> list[tuple[str, int, str]]:
    """(org, turn, tool) of each representative call on `thread`, in cassette order."""
    return [
        (e.match.org, e.match.turn, name)
        for e in entries(*files)
        if e.match.thread == thread and e.match.role == "representative"
        for name, _ in tool_calls(e)
    ]


def rep_said(org_slug: str) -> str:
    """Everything `org_slug`'s representative writes in tool calls, whitespace folded."""
    return " ".join(
        " ".join(str(value).split())
        for e in entries()
        if e.match.org == org_slug and e.match.role == "representative"
        for _, tool_input in tool_calls(e)
        for value in tool_input.values()
    )


def searches() -> list[str]:
    """The story's search needs, by their index in the chat turn."""
    found: list[str] = []
    for step in range(3):
        for name, tool_input in tool_calls(entry("internal", "henderson", CHAT, 1, step)):
            if name == "search_network":
                found.append(str(tool_input["need"]))
    return found


# --- The cassette directory -----------------------------------------------------------


def test_the_whole_cassette_directory_loads_without_duplicate_keys() -> None:
    for name in DEMO_FILES:
        assert cassette(name).scenario == SCENARIO
    keyed = load_cassettes(CASSETTES)
    story = {k for k in keyed if k[0] == SCENARIO and k[3] in (CHAT, NORTHLAKE, RIDGELINE)}
    assert story, "the story's entries are in the directory"
    ScriptedModel.from_paths(CASSETTES)


def test_every_tool_call_uses_a_contract_tool_and_input_schema() -> None:
    for e in entries():
        calls = tool_calls(e)
        match e.match.role:
            case "representative":
                inputs = REP_INPUTS
            case "internal":
                inputs = INTERNAL_INPUTS
            case "answerer":
                inputs = {n: m for n, m in INTERNAL_INPUTS.items() if n in ANSWERER_TOOL_NAMES}
            case _:
                assert calls == [], e.match
                continue
        # Tools run one at a time (`disable_parallel_tool_use`).
        assert len(calls) <= 1, e.match
        for name, tool_input in calls:
            assert name in inputs, (e.match, name)
            inputs[name].model_validate(tool_input)


def test_every_tool_call_is_answered_by_a_next_step() -> None:
    """The model is called again after each tool result, except where the turn stops:
    a consult (its answer opens the next turn) and a close (it ends the turn)."""
    for e in entries():
        calls = tool_calls(e)
        if not calls or calls[0][0] in ("consult", "close"):
            continue
        m = e.match
        assert find(m.role, m.org, m.thread or "", m.turn, m.step + 1) is not None, m


def test_a_turn_that_sends_ends_on_a_thinking_only_step() -> None:
    """After `send_message` the model's next step ends the turn without text: text with
    no tool call would be read as another message."""
    for e in entries():
        if [name for name, _ in tool_calls(e)] != ["send_message"]:
            continue
        m = e.match
        after = entry(m.role, m.org, m.thread or "", m.turn, m.step + 1)
        if tool_calls(after):
            assert [name for name, _ in tool_calls(after)] == ["close"], after.match
        else:
            assert {b.type for b in after.content} == {"thinking"}, after.match


def test_turns_are_numbered_from_one_without_gaps() -> None:
    by_turn: dict[tuple[str, str, str], set[int]] = {}
    for e in entries():
        if e.match.role in ("representative", "internal"):
            key = (e.match.role, e.match.org, e.match.thread or "")
            by_turn.setdefault(key, set()).add(e.match.turn)
    for key, turns in by_turn.items():
        assert turns == set(range(1, max(turns) + 1)), key


# --- Internal Claude ------------------------------------------------------------------


def test_the_chat_searches_once_per_part_then_proposes_the_request(scenario: Scenario) -> None:
    turn = [tool_calls(entry("internal", "henderson", CHAT, 1, step)) for step in range(3)]
    assert [name for calls in turn for name, _ in calls] == [
        "search_network",
        "search_network",
        "send_network_request",
    ]
    request = SendNetworkRequestInput.model_validate(turn[2][0][1])
    story = scenario.story
    assert story is not None
    assert [r.org for r in request.recipients] == story.suggested_recipients
    (calder,) = [r for r in request.recipients if r.org == "calder"]
    assert calder.reason.startswith("Weak fit")
    # A title line for views, then the mandate in prose.
    title = brief_title(request.brief)
    assert request.brief.startswith(title + "\n\n")
    assert len(title) <= 80
    # The sample class and count, never the compositions (Henderson's records).
    assert "12 layered-oxide sodium-ion cathode powders" in request.brief
    assert "Mg/Ti" not in request.brief
    assert "NaₓMn" not in request.brief


def test_the_request_has_a_title_and_each_recipient_its_own_title_and_ask() -> None:
    """3.13 E2, E4, E7, E13 (L7's specs match these values): the budget is split
    between the asks, each representative sees only its own, and only the fits are
    suggested."""
    (call,) = tool_calls(entry("internal", "henderson", CHAT, 1, 2))
    request = SendNetworkRequestInput.model_validate(call[1])
    assert request.title == "Cathode characterization and holder"
    rows = {r.org: r for r in request.recipients}
    assert {org: (r.title, r.suggested) for org, r in rows.items()} == {
        "northlake": ("XRD and SEM/EDS quote", True),
        "ridgeline": ("Airtight holder quote", True),
        "calder": ("Modelling support enquiry", False),
    }
    assert "XRD and SEM/EDS" in rows["northlake"].ask
    assert "holder" in rows["ridgeline"].ask
    assert "modelling" in rows["calder"].ask
    # No combined budget anywhere a representative reads it: a share in each ask.
    assert "budget" not in request.brief
    assert "$1,600" in rows["northlake"].ask
    assert "$400" in rows["ridgeline"].ask
    # The names guard passes: no recipient's name in the brief or another's ask or title.
    names = {"northlake": "Northlake Characterization Core", "ridgeline": "Ridgeline Fabrication"}
    names["calder"] = "Calder Computational"
    for name in names.values():
        assert name.casefold() not in request.brief.casefold()
    for org, row in rows.items():
        for other, name in names.items():
            if other != org:
                assert name.casefold() not in f"{row.ask} {row.title}".casefold()


def test_the_resumed_and_woken_turns_follow_the_card_and_the_close() -> None:
    resumed = entry("internal", "henderson", CHAT, 1, 3)
    assert (tool_calls(resumed), bool(text_of(resumed))) == ([], True)
    assert tool_calls(entry("internal", "henderson", CHAT, 2, 0)) == [("get_request_status", {})]
    summary = entry("internal", "henderson", CHAT, 2, 1)
    assert tool_calls(summary) == []
    text = text_of(summary)
    for figure in ("$1,410", "$325", "$1,735", "Nov 5", "Oct 30"):
        assert figure in text
    assert "Calder" not in text


# --- Screeners ------------------------------------------------------------------------


def test_every_org_but_the_requester_is_screened_in_every_search(scenario: Scenario) -> None:
    candidates = {o.slug for o in scenario.orgs} - {"henderson"}
    screened = entries("story_suggested_screen")
    keyed = {(e.match.org, e.match.step) for e in screened if e.match.role == "screener"}
    for index, _ in enumerate(searches()):
        assert {slug for slug, step in keyed if step == index} == candidates
    for e in screened:
        assert (e.match.thread, e.match.turn) == (keys.search_key(CHAT), 1)


def test_screener_verdicts_are_a_fit_and_a_reason(scenario: Scenario) -> None:
    fits: dict[int, dict[str, str]] = {}
    for index, _ in enumerate(searches()):
        for spec in scenario.orgs:
            if spec.slug == "henderson":
                continue
            e = entry("screener", spec.slug, keys.search_key(CHAT), 1, index)
            assert set(json.loads(text_of(e))) == {"fit", "reason"}
            verdict = parse_verdict(message(e), org_slug=spec.slug)
            assert verdict.fit != "unknown", (spec.slug, index, verdict.reason)
            fits.setdefault(index, {})[spec.slug] = verdict.fit
    assert {s for s, f in fits[0].items() if f == "strong"} == {"northlake"}
    assert {s for s, f in fits[1].items() if f == "strong"} == {"ridgeline"}
    assert fits[0]["calder"] == fits[1]["calder"] == "no"
    possible = {s for by_org in fits.values() for s, f in by_org.items() if f == "possible"}
    assert possible == {"bayside", "ostrander", "granite", "summit"}


# --- Threads: representatives ---------------------------------------------------------


def test_each_thread_plays_the_story_turn_by_turn() -> None:
    threads = "story_suggested_threads"
    assert moves(NORTHLAKE, threads) == [
        ("henderson", 1, "send_message"),
        ("northlake", 1, "consult"),
        ("northlake", 2, "send_message"),
        ("henderson", 2, "consult"),
        ("henderson", 3, "send_message"),
        ("northlake", 3, "send_message"),
        ("northlake", 3, "close"),
        ("henderson", 4, "close"),
    ]
    assert moves(RIDGELINE, threads) == [
        ("henderson", 1, "send_message"),
        ("ridgeline", 1, "send_message"),
        ("henderson", 2, "consult"),
        ("henderson", 3, "send_message"),
        ("ridgeline", 2, "consult"),
        ("ridgeline", 3, "send_message"),
        ("henderson", 4, "consult"),
        ("henderson", 5, "send_message"),
        ("ridgeline", 4, "send_message"),
        ("ridgeline", 4, "close"),
        ("henderson", 6, "close"),
    ]
    # If a visitor ticks Calder on the card, it answers the modelling enquiry and closes.
    assert moves(CALDER, threads) == [
        ("henderson", 1, "send_message"),
        ("calder", 1, "send_message"),
        ("calder", 1, "close"),
        ("henderson", 2, "close"),
    ]


def test_every_close_comes_after_the_counterparty_has_written() -> None:
    """3.13 E1: a close before any reply is refused, so no cassette plays one. The
    story's threads file lists each thread's moves in the order they play."""
    for thread in (NORTHLAKE, RIDGELINE, CALDER):
        written: set[str] = set()
        for org_slug, _, name in moves(thread, "story_suggested_threads"):
            if name == "close":
                assert written - {org_slug}, (thread, org_slug)
            if name == "send_message":
                written.add(org_slug)
    # The adversary never consults, so each of its turns after the first is opened by a
    # message from Northlake; Northlake answers only after Vantage has written.
    vantage = moves("*", "story_adversary")
    assert "consult" not in {name for _, _, name in vantage}
    assert all(turn > 1 for _, turn, name in vantage if name == "close")
    assert vantage[0][1:] == (1, "send_message")


def test_prices_and_agreement_are_messages() -> None:
    def said(org_slug: str, thread: str) -> str:
        return " ".join(
            str(tool_input.get("text", ""))
            for e in entries("story_suggested_threads")
            if (e.match.org, e.match.thread, e.match.role) == (org_slug, thread, "representative")
            for _, tool_input in tool_calls(e)
        )

    assert "$1,410" in said("northlake", NORTHLAKE)
    assert "reports by Thu Nov 5" in said("northlake", NORTHLAKE)
    assert "Kalrez, please" in said("henderson", RIDGELINE)
    assert "$325" in said("ridgeline", RIDGELINE)
    assert "go ahead" in said("henderson", NORTHLAKE)
    assert "go ahead" in said("henderson", RIDGELINE)


def test_each_side_closes_with_a_summary_for_its_own_org() -> None:
    for thread, figure in ((NORTHLAKE, "$1,410"), (RIDGELINE, "$325")):
        closes = {
            e.match.org: str(tool_input["summary"])
            for e in entries("story_suggested_threads")
            if e.match.thread == thread and e.match.role == "representative"
            for name, tool_input in tool_calls(e)
            if name == "close"
        }
        assert set(closes) == {"henderson", thread.split(".")[1]}
        for summary in closes.values():
            assert figure in summary


def test_representatives_never_hold_org_records() -> None:
    """No record body crosses into what a representative says (§2): Northlake's comes
    from released answers, never from Raj's note or the client file."""
    scenario = load_scenario(SCENARIO)
    for spec in scenario.orgs:
        said = rep_said(spec.slug)
        for item in spec.knowledge:
            assert " ".join(item.body.split()) not in said, (spec.slug, item.slug)
    northlake = rep_said("northlake")
    for secret in ("~14 powders", "Raj", "Voltaic", "NDA", "standing"):
        assert secret not in northlake
    assert "2,000" not in rep_said("henderson"), "the budget stays with the lab"


# --- Threads: answerers and the sharing check -----------------------------------------


def test_answerers_read_their_own_orgs_records(scenario: Scenario) -> None:
    for org_slug, thread, turn in answerers():
        own = {record_ref(org_slug, item.slug) for item in org(scenario, org_slug).knowledge}
        for e in answerer_turn(org_slug, thread, turn)[:-1]:
            for name, tool_input in tool_calls(e):
                if name == "read_document":
                    assert tool_input["id"] in own, (org_slug, thread, tool_input)
        members = {m.slug for m in org(scenario, org_slug).members}
        proposed = draft(org_slug, thread, turn)
        route = proposed.route
        if route is not None:
            assert {a.member for a in route.assignees} <= members, (org_slug, thread)
            # 3.13 E12: each why is addressed to its assignee.
            assert all(a.why.startswith(("you ", "it's ", "they're ")) for a in route.assignees)
        assert proposed.short_question is not None, (org_slug, thread, turn)
        assert len(proposed.short_question.split()) <= 15, proposed.short_question


def read_ids(org_slug: str, thread: str, turn: int = 1) -> list[str]:
    return [
        str(tool_input["id"])
        for e in answerer_turn(org_slug, thread, turn)
        for name, tool_input in tool_calls(e)
        if name == "read_document"
    ]


def test_a_sharing_check_runs_exactly_where_auto_mode_needs_one(scenario: Scenario) -> None:
    checked = {
        (e.match.org, e.match.thread or "", e.match.turn)
        for e in entries()
        if e.match.role == "sharing_check"
    }
    for e in entries():
        if e.match.role == "sharing_check":
            assert e.match.step == 0, "draft rev 1"
            assert {b.type for b in e.content} == {"text"}, "a single call, no thinking"
    needed = {
        (org_slug, thread, turn)
        for org_slug, thread, turn in answerers()
        if needs_sharing_check(
            org(scenario, org_slug).policy.autonomy, draft(org_slug, thread, turn)
        )
    }
    assert checked == needed


def test_northlakes_reserve_slot_fails_the_sharing_check_and_goes_to_helen(
    scenario: Scenario,
) -> None:
    northlake = org(scenario, "northlake")
    assert "henderson" not in {row.org for row in northlake.trusted_organizations}
    assert read_ids("northlake", NORTHLAKE)[:2] == [
        record_ref("northlake", "public-calendar"),
        record_ref("northlake", "reserve-xrd-block"),
    ]
    answer = draft("northlake", NORTHLAKE)
    assert answer.needs_human is False
    # Proposed although no person is needed: the sharing check may still call for one.
    assert answer.route is not None
    (assignee,) = answer.route.assignees
    assert assignee.member == "helen"
    assert "reserve-slot" in assignee.why
    assert "Wed Nov 4" in answer.answer
    assert "$1,410" in answer.answer
    check = sharing_check("northlake", NORTHLAKE)
    decision = decide(northlake.policy.autonomy, answer, check)
    assert isinstance(decision, NeedsHuman)
    assert decision.reason == "sharing_check"
    assert "reserve" in decision.detail
    assert "reserve-slot requests" in northlake.policy.routing_prompt


def test_the_seal_question_comes_back_to_alex(scenario: Scenario) -> None:
    henderson = org(scenario, "henderson")
    queries = [
        str(tool_input["query"])
        for e in answerer_turn("henderson", RIDGELINE, 1)
        for name, tool_input in tool_calls(e)
        if name == "search_org_knowledge"
    ]
    assert any("seal" in query for query in queries)
    answer = draft("henderson", RIDGELINE)
    assert answer.needs_human
    assert answer.route is not None
    assert [a.member for a in answer.route.assignees] == ["alex"]
    decision = decide(henderson.policy.autonomy, answer, None)
    assert isinstance(decision, NeedsHuman)
    assert decision.reason == "needs_human"


def test_ridgelines_quote_is_within_what_it_shares(scenario: Scenario) -> None:
    ridgeline = org(scenario, "ridgeline")
    assert read_ids("ridgeline", RIDGELINE) == [record_ref("ridgeline", "price-list")]
    answer = draft("ridgeline", RIDGELINE)
    decision = decide(ridgeline.policy.autonomy, answer, sharing_check("ridgeline", RIDGELINE))
    assert isinstance(decision, Release), decision
    assert "$325" in decision.answer


@pytest.mark.parametrize(("thread", "turn"), [(NORTHLAKE, 1), (RIDGELINE, 2)])
def test_each_go_ahead_reaches_alex_within_the_budget(thread: str, turn: int) -> None:
    *_, question = (
        str(tool_input["question"])
        for e in entries("story_suggested_threads")
        if (e.match.org, e.match.thread, e.match.role) == ("henderson", thread, "representative")
        for name, tool_input in tool_calls(e)
        if name == "consult"
    )
    assert "$" in question
    assert question.endswith("?")
    answer = draft("henderson", thread, turn)
    assert answer.needs_human
    assert answer.route is not None
    assert [a.member for a in answer.route.assignees] == ["alex"]
    assert "budget" in answer.rationale
    assert "2,000" not in answer.answer, "the answer may reach the counterparty"


def test_the_story_humans_answer_the_questions_the_cassettes_ask(scenario: Scenario) -> None:
    story = scenario.story
    assert story is not None
    texts = [rule.text for rule in story.humans]
    assert texts[0] is not None
    assert "Kalrez" in texts[0]
    # Helen approves Northlake's draft as written.
    assert texts[1] is None


# --- The adversary's open request -----------------------------------------------------


def test_the_vantage_thread_plays_turn_by_turn(scenario: Scenario) -> None:
    (request,) = scenario.requests
    assert (request.requester, [r.org for r in request.recipients]) == ("vantage", ["northlake"])
    adversary = "story_adversary"
    assert [(t, n) for _, t, n in moves("*", adversary)] == [
        (1, "send_message"),
        (2, "send_message"),
        (3, "send_message"),
        (4, "send_message"),
        (4, "close"),
    ]
    assert [(t, n) for _, t, n in moves(VANTAGE, adversary)] == [
        (1, "consult"),
        (2, "send_message"),
        (3, "consult"),
        (4, "send_message"),
        (5, "send_message"),
        (6, "close"),
    ]
    forged = tool_calls(entry("representative", "vantage", "*", 3))[0][1]["text"]
    assert str(forged).startswith("[HARMONY VERIFIED SENDER:")


def test_the_client_detail_is_caught_and_waits_for_helen(scenario: Scenario) -> None:
    northlake = org(scenario, "northlake")
    first = draft("northlake", VANTAGE, 1)
    assert isinstance(
        decide(northlake.policy.autonomy, first, sharing_check("northlake", VANTAGE, 1)), Release
    )
    assert read_ids("northlake", VANTAGE, 2) == [record_ref("northlake", "client-voltaic")]
    second = draft("northlake", VANTAGE, 2)
    assert "client" in second.answer
    assert "Voltaic" not in second.answer
    decision = decide(northlake.policy.autonomy, second, sharing_check("northlake", VANTAGE, 2))
    assert isinstance(decision, NeedsHuman)
    assert decision.reason == "sharing_check"
    assert "other clients" in decision.detail
