"""The demo scenario's content and story format (`story.yaml`, `seeds.schema.StorySpec`):
what each org says about itself and shares, the records the story depends on, the
story's reference checks, date templating, the seeded requests' titles, and the open
requests a world is seeded with (threads.yaml `requests`)."""

from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.agents import keys
from harmony.agents.models import EscalationAction, brief_title
from harmony.db import repo
from harmony.db.models import NetworkRequest, Thread
from harmony.domain.requests import recipients_of
from harmony.seeds.dates import date_templates, render_dates, short_date
from harmony.seeds.ids import seed_id
from harmony.seeds.loader import SCENARIOS_DIR, Scenario, load_scenario, load_world
from harmony.seeds.schema import (
    Closed,
    InlineQuestion,
    OrgSpec,
    OutreachApproved,
    Resolved,
    Routed,
    ScenarioError,
    StoryAction,
)

ANCHOR = date(2026, 10, 26)


def _copy_demo(root: Path) -> Path:
    shutil.copytree(SCENARIOS_DIR / "harmony-demo", root / "harmony-demo")
    return root / "harmony-demo" / "story.yaml"


def _edit_story(root: Path, edit: dict[str, Any]) -> None:
    path = _copy_demo(root)
    story = yaml.safe_load(path.read_text(encoding="utf-8"))
    story.update(edit)
    path.write_text(yaml.safe_dump(story, sort_keys=False, allow_unicode=True))


def _problems(root: Path, file: str = "story.yaml") -> list[str]:
    """`<path>: <message>` for each problem, all of them in `file`."""
    with pytest.raises(ScenarioError) as caught:
        load_scenario("harmony-demo", root)
    assert all(problem.file.endswith(file) for problem in caught.value.problems)
    return [f"{problem.path}: {problem.message}" for problem in caught.value.problems]


def _edit_requests(root: Path, requests: list[dict[str, Any]]) -> None:
    _copy_demo(root)
    path = root / "harmony-demo" / "threads.yaml"
    threads = yaml.safe_load(path.read_text(encoding="utf-8"))
    threads["requests"] = requests
    path.write_text(yaml.safe_dump(threads, sort_keys=False, allow_unicode=True))


def _org(scenario: Scenario, slug: str) -> OrgSpec:
    return next(org for org in scenario.orgs if org.slug == slug)


def _records(scenario: Scenario, org: str) -> dict[str, str]:
    return {item.slug: " ".join(item.body.split()) for item in _org(scenario, org).knowledge}


def _flat(text: str) -> str:
    return " ".join(text.split())


# --- The demo story -------------------------------------------------------------------


def test_the_demo_story_loads_with_its_scenario() -> None:
    scenario = load_scenario("harmony-demo")
    story = scenario.story
    assert story is not None
    assert (story.name, story.member) == ("suggested", "alex")
    assert story.suggested_recipients == ["northlake", "ridgeline", "calder"]
    assert render_dates(story.suggested_message, scenario.spec.anchor).count("by Nov 6,") == 1
    beats = {step.id: step.expect for step in story.steps if step.expect is not None}
    # The main path asks a person twice: the outreach, and Alex's review of Northlake's
    # quote. Ridgeline's conversation closes with nobody asked.
    assert beats == {
        "approve-outreach": OutreachApproved(beat="outreach_approved"),
        "records-answer": Closed(beat="closed", orgs=["ridgeline"]),
        "review-quote": InlineQuestion(beat="inline_question", org="northlake", member="alex"),
        "review-answer": Resolved(
            beat="resolved", org="henderson", member="alex", action="approve"
        ),
        "closed": Closed(beat="closed", orgs=["northlake", "ridgeline"]),
    }
    assert not [b for b in beats.values() if isinstance(b, Routed)], "no one else's inbox"
    # The one review: Alex approves the recommendation as drafted.
    assert [(r.org, r.counterparty, r.member, r.action, r.text) for r in story.humans] == [
        ("henderson", "northlake", "alex", "approve", None)
    ]


def test_the_story_hedges_what_live_models_may_do() -> None:
    """Live models play every side in the hosted demo, so the steps say what is likely
    and point at what the visitor can open (3.13)."""
    story = load_scenario("harmony-demo").story
    assert story is not None
    bodies = {step.id: _flat(step.body) for step in story.steps}
    assert bodies["approve-outreach"].startswith(
        "Read the brief and each organization's ask: the shared brief gives the sample "
        "class and count, and each ask holds that organization's part and its limit. Edit "
        "either if you like."
    )
    assert bodies["what-it-saw"].startswith(
        "Open the request from the sidebar, then Details → What our representative saw"
    )
    for step_id in (
        "ask",
        "records-answer",
        "review-quote",
        "review-answer",
        "vantage-probe",
    ):
        assert "likely" in bodies[step_id], step_id
    # The figures are the list prices', and the story says they're likely, not booked.
    assert "about $325" in bodies["records-answer"]
    assert "about $1,284" in bodies["review-quote"]
    assert "about $1,434" in bodies["review-quote"]
    assert "about $1,759" in bodies["closed"]


def test_every_seeded_request_has_a_title() -> None:
    scenario = load_scenario("harmony-demo")
    titles = {thread.slug: thread.request.title for thread in scenario.threads}
    assert titles == {
        "henderson-northlake-precursor-xrd": "Powder XRD on 6 precursors",
        "ridgeline-northlake-seal-sem": "SEM on argon-stored seals",
    }
    (probe,) = scenario.requests
    assert probe.title == "Rush SEM slot enquiry"
    (recipient,) = probe.recipients
    assert (recipient.org, recipient.title) == ("northlake", "Rush SEM slot enquiry")
    assert recipient.ask is not None
    assert "Thursday" in recipient.ask


def test_a_seeded_message_runs_long_enough_to_check_it_shows_in_full() -> None:
    """The web's requests spec opens the seal thread as Raj to check that a long message
    shows in full, not cut short: one of at least 900 characters, many lines at any
    width. Its text is free."""
    scenario = load_scenario("harmony-demo")
    seal = next(t for t in scenario.threads if t.slug == "ridgeline-northlake-seal-sem")
    assert max(len(m.body) for m in seal.messages if m.sender == "northlake") >= 900


def test_the_demo_network_has_the_featured_and_background_orgs() -> None:
    scenario = load_scenario("harmony-demo")
    featured = {o.slug for o in scenario.orgs if o.featured}
    assert featured == {"henderson", "northlake", "ridgeline", "calder", "vantage"}
    assert len(scenario.orgs) - len(featured) == 10


@pytest.mark.parametrize("name", ["harmony-demo", "minimal"])
def test_every_org_describes_itself_and_what_it_shares_in_prose(name: str) -> None:
    for org in load_scenario(name).orgs:
        assert org.settings.public_summary.strip(), org.slug
        assert org.settings.instructions.strip(), org.slug
        assert org.policy.sharing_description.strip(), org.slug
        for member in org.members:
            assert member.profile.strip(), f"{org.slug}/{member.slug}"


def test_northlake_shares_its_published_terms_but_not_other_clients() -> None:
    northlake = _org(load_scenario("harmony-demo"), "northlake")
    assert _flat(northlake.policy.sharing_description) == (
        "Our external academic price list with its published fees and terms, what is open "
        "on the public instrument calendar and how booking works, the customer's own quote "
        "and booking, sample-prep, air-free handling and delivery requirements (our "
        "glovebox, who receives deliveries, what we supply, and which holders fit our "
        "instruments, Ridgeline's included), and typical turnaround. Not: other clients or "
        "their bookings, staff notes, or off-list pricing."
    )


def test_the_trusted_organization_example_leaves_the_story_to_the_sharing_check() -> None:
    """Northlake trusts Ridgeline with its airlock dimensions, in its default mode.
    Henderson is trusted by no one, so its consults meet each org's default."""
    scenario = load_scenario("harmony-demo")
    trusted = [(org.slug, row) for org in scenario.orgs for row in org.trusted_organizations]
    assert [(slug, row.org, row.autonomy) for slug, row in trusted] == [
        ("northlake", "ridgeline", None)
    ]
    ((_, row),) = trusted
    assert _flat(row.sharing_description).startswith("Our Apreo 2 airlock dimensions")
    assert "Apreo 2 airlock" in _records(scenario, "northlake")["apreo-airlock"]


def test_the_records_hold_what_the_story_turns_on() -> None:
    """The cassettes' answerers read these: Henderson's records answer what the
    counterparties ask (instrument, seal, samples, dates), Northlake's published
    calendar and terms give both options, and the list prices add up to the quotes
    ($1,284 standard, $1,434 priority, $325, $1,759 with priority)."""
    scenario = load_scenario("harmony-demo")
    henderson = _records(scenario, "henderson")
    assert "Thermo Fisher Apreo 2 airlock, with a Kalrez seal" in henderson["transfer-holder"]
    assert "about 150 mg each, are in crimp-sealed glass vials" in henderson["samples-handling"]
    assert "alex.chen@henderson-lab.example" in henderson["samples-handling"]
    dates = henderson["dates"]
    assert "results by Fri Nov 6, for the group meeting on Mon Nov 9" in dates
    assert "Results by Fri Nov 13 still work" in dates
    assert "worth up to about $200 extra" in dates
    assert "About $2,000 discretionary" in henderson["budget"]
    assert "Alex reviews any single order over $1,000" in henderson["budget"]
    assert "ready now" in henderson["samples-handling"]
    assert "One holder is enough" in henderson["transfer-holder"]

    northlake = _records(scenario, "northlake")
    calendar = northlake["public-calendar"]
    assert "Next standard XRD block: Tue Nov 10, with reports by Fri Nov 13." in calendar
    priority = "Wed Nov 4, 09:00–13:00, for up to 14 powders, with reports by Thu Nov 5"
    assert priority in calendar
    assert "a session on Wed Nov 4 reports by Fri Nov 6" in calendar
    prices = northlake["price-list"]
    assert "Powder XRD $45 per sample. SEM/EDS $60 per sample." in prices
    assert "Air-free transfer and handling $150 flat per batch: one fee" in prices
    assert "10 or more samples on one technique get 10% off that technique" in prices
    assert "a priority XRD block is a $150 flat fee" in prices
    assert "Helen decides only off-list exceptions." in northlake["pricing-policy"]
    assert "O2 and H2O held below 0.5 ppm" in northlake["glovebox-receiving"]
    assert "Room 112, Physical Sciences Building" in northlake["glovebox-receiving"]
    # Who freed the Nov 4 block: a client, so it stays out of every answer.
    assert "Voltaic" in northlake["nov-4-xrd-block"]
    standard = (12 * 45 + 12 * 60) * 9 // 10 + 150
    assert standard == 1_284
    assert standard + 150 == 1_434

    ridgeline = _records(scenario, "ridgeline")["price-list"]
    assert "carries one standard SEM stub (any number of powder mounts on it): $240" in ridgeline
    assert "RT-A2 sealed SEM transfer holder" in ridgeline
    assert "Kalrez seal in its place +$85" in ridgeline
    assert "Free delivery on Northlake campus the next business day" in ridgeline
    assert 240 + 85 == 325
    assert 1_434 + 325 == 1_759
    # Ordered on the anchor, Mon Oct 26: ready Fri Oct 30, delivered Mon Nov 2.
    assert (ANCHOR + timedelta(days=4), ANCHOR + timedelta(days=7)) == (
        date(2026, 10, 30),
        date(2026, 11, 2),
    )


def test_the_story_dates_fall_on_the_weekdays_the_content_names() -> None:
    days = {
        "Tue Nov 3": date(2026, 11, 3),
        "Wed Nov 4": date(2026, 11, 4),
        "Thu Nov 5": date(2026, 11, 5),
        "Fri Nov 6": date(2026, 11, 6),
        "Mon Nov 9": date(2026, 11, 9),
        "Tue Nov 10": date(2026, 11, 10),
        "Fri Nov 13": date(2026, 11, 13),
        "Mon Nov 2": date(2026, 11, 2),
    }
    for name, day in days.items():
        assert day.strftime("%a") == name[:3], name
    assert ANCHOR.strftime("%a") == "Mon"


def test_a_scenario_without_a_story_has_none(tmp_path: Path) -> None:
    (_copy_demo(tmp_path)).unlink()
    assert load_scenario("harmony-demo", tmp_path).story is None


def test_story_actions_are_the_escalation_actions() -> None:
    # Restated in seeds.schema to keep seeds free of the agents package (import cycle).
    assert get_args(StoryAction) == get_args(EscalationAction)


# --- Reference checks -----------------------------------------------------------------


def test_story_references_must_resolve(tmp_path: Path) -> None:
    _edit_story(
        tmp_path,
        {
            "as": "nobody",
            "suggested_recipients": ["northlake", "atlantis"],
            "humans": [{"org": "northlake", "as": "alex", "counterparty": "northlake"}],
        },
    )
    problems = _problems(tmp_path)
    assert "as: no member 'nobody'" in problems
    assert "suggested_recipients[1]: no org 'atlantis'" in problems
    assert "humans[0].as: 'alex' is not a member of 'northlake'" in problems
    assert "humans[0].counterparty: an escalation's counterparty is another org" in problems


def test_story_steps_are_checked(tmp_path: Path) -> None:
    steps = [
        {"id": "a", "title": "A", "body": "Until {next week}.", "act_as": "ghost"},
        {
            "id": "a",
            "title": "B",
            "body": "B.",
            "expect": {"beat": "routed", "org": "northlake", "member": "alex"},
        },
        {"id": "c", "title": "C", "body": "C.", "expect": {"beat": "closed", "orgs": ["atlantis"]}},
    ]
    _edit_story(tmp_path, {"steps": steps, "suggested_message": "By {tomorrow}."})
    problems = _problems(tmp_path)
    assert "steps[0].act_as: no member 'ghost'" in problems
    assert "steps[0].body: {next week} is not a relative date such as {+11d}" in problems
    assert "steps[1].id: duplicate 'a'" in problems
    assert "steps[1].expect.member: 'alex' is not a member of 'northlake'" in problems
    assert "steps[2].expect.orgs[0]: no org 'atlantis'" in problems
    assert "suggested_message: {tomorrow} is not a relative date such as {+11d}" in problems


@pytest.mark.parametrize(
    "expect", [{"beat": "teleported"}, {"beat": "accepted", "orgs": ["northlake"]}]
)
def test_an_unknown_beat_is_a_parse_error(tmp_path: Path, expect: dict[str, Any]) -> None:
    steps = [{"id": "a", "title": "A", "body": "A.", "expect": expect}]
    _edit_story(tmp_path, {"steps": steps})
    assert any(p.startswith("steps[0].expect") for p in _problems(tmp_path))


# --- Dates ----------------------------------------------------------------------------


def test_date_templates_render_as_short_dates() -> None:
    assert date_templates("by {+11d}, or {+0d}") == ["+11d", "+0d"]
    assert render_dates("by {+11d}, or {+0d}", ANCHOR) == "by Nov 6, or Oct 26"
    assert short_date(date(2027, 1, 2)) == "Jan 2"
    with pytest.raises(ValueError, match="relative date such as"):
        render_dates("by {soon}", ANCHOR)


# --- Open requests --------------------------------------------------------------------


def _request(**overrides: Any) -> dict[str, Any]:
    return {
        "slug": "probe",
        "requester": "vantage",
        "created_by": "jordan",
        "brief": "SEM capacity",
        "recipients": [{"org": "northlake", "reason": "SEM."}],
        **overrides,
    }


def test_open_request_references_must_resolve(tmp_path: Path) -> None:
    _edit_requests(
        tmp_path,
        [
            _request(
                created_by="helen",
                recipients=[
                    {"org": "vantage", "reason": "Itself."},
                    {"org": "atlantis", "reason": "Nowhere."},
                    {"org": "northlake", "reason": "SEM."},
                    {"org": "northlake", "reason": "Again."},
                ],
            ),
            _request(slug="henderson-northlake-precursor-xrd", requester="atlantis"),
        ],
    )
    problems = _problems(tmp_path, "threads.yaml")
    assert "requests[0].created_by: no member 'helen' in org 'vantage'" in problems
    assert "requests[0].recipients[0].org: a request goes to other orgs" in problems
    assert "requests[0].recipients[1].org: no org 'atlantis'" in problems
    assert "requests[0].recipients[3].org: duplicate 'northlake'" in problems
    assert "requests[1].slug: duplicate 'henderson-northlake-precursor-xrd'" in problems
    assert "requests[1].requester: no org 'atlantis'" in problems


def test_seeded_briefs_lead_with_a_title_line() -> None:
    scenario = load_scenario("harmony-demo")
    briefs = [thread.request.brief for thread in scenario.threads]
    briefs += [request.brief for request in scenario.requests]
    for brief in briefs:
        title, blank, *body = brief.split("\n")
        assert (blank, brief_title(brief)) == ("", title), brief
        assert body, brief


async def test_the_vantage_request_is_seeded_open_as_an_approved_outreach_leaves_one(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    scenario = load_scenario("harmony-demo")
    (spec,) = scenario.requests
    world_id = await load_world(db_sessionmaker, scenario, build_sha="test")
    async with db_sessionmaker() as session:
        request = await repo.get_required(
            session, NetworkRequest, world_id, seed_id(scenario.name, "network_request", spec.slug)
        )
        threads = await session.scalars(
            select(Thread.id).where(Thread.world_id == world_id, Thread.request_id == request.id)
        )
        assert list(threads) == [], "its workflow opens the threads"
    assert (request.status, request.conversation_id) == ("active", None)
    assert request.requester_org_id == seed_id(scenario.name, "org", "vantage")
    assert request.title == "Rush SEM slot enquiry"
    # The requester representative's whole mandate, as approved.
    assert request.brief == spec.brief
    assert brief_title(request.brief) == (
        "Rush SEM capacity for a battery OEM client, Thursday afternoons preferred"
    )
    (recipient,) = recipients_of(request)
    assert recipient.org_id == seed_id(scenario.name, "org", "northlake")
    (spec_recipient,) = spec.recipients
    assert (recipient.title, recipient.ask) == (spec_recipient.title, spec_recipient.ask)
    assert recipient.title == "Rush SEM slot enquiry"
    # Its thread's cassette key, which story_adversary.yaml plays.
    assert keys.thread_key("vantage", "northlake") == "vantage.northlake"


async def test_a_seeded_thread_is_titled_by_its_request(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world_id = await load_world(db_sessionmaker, "harmony-demo", build_sha="test")
    async with db_sessionmaker() as session:
        rows = await session.execute(
            select(Thread.id, Thread.title).where(Thread.world_id == world_id)
        )
        titles = dict(rows.all())
    assert titles == {
        seed_id("harmony-demo", "thread", "henderson-northlake-precursor-xrd"): (
            "Powder XRD on 6 precursors"
        ),
        seed_id("harmony-demo", "thread", "ridgeline-northlake-seal-sem"): (
            "SEM on argon-stored seals"
        ),
    }
