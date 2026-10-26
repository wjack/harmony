"""The demo scenario's content and story format (`story.yaml`, `seeds.schema.StorySpec`):
what each org says about itself and shares, the records the story depends on, the
story's reference checks, date templating, and the open requests a world starts with
(threads.yaml `requests`)."""

from __future__ import annotations

import shutil
from datetime import date
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
    beats = {step.id: step.expect for step in story.steps}
    assert beats["seal-question"] == InlineQuestion(
        beat="inline_question", org="ridgeline", member="alex"
    )
    assert beats["seal-answer"] == Resolved(
        beat="resolved", org="henderson", member="alex", action="approve"
    )
    assert beats["northlake-routed"] == Routed(beat="routed", org="northlake", member="helen")
    assert beats["northlake-approved"] == Resolved(
        beat="resolved", org="northlake", member="helen", action="approve"
    )
    assert beats["go-ahead"] == InlineQuestion(
        beat="inline_question", org="northlake", member="alex"
    )
    assert beats["closed"] == Closed(beat="closed", orgs=["northlake", "ridgeline"])
    # In order: the seal question and Ridgeline's go-ahead are two decisions.
    assert [(r.org, r.counterparty, r.member, r.action, r.text) for r in story.humans] == [
        (
            "henderson",
            "ridgeline",
            "alex",
            "approve",
            "Kalrez, please. The powders may sit under argon for a few weeks.",
        ),
        ("northlake", "henderson", "helen", "approve", None),
        ("henderson", "northlake", "alex", "approve", "Yes, go ahead with Northlake."),
        ("henderson", "ridgeline", "alex", "approve", "Yes, go ahead with Ridgeline."),
    ]


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


def test_northlake_shares_neither_reserve_slots_nor_other_clients_nor_discounts() -> None:
    northlake = _org(load_scenario("harmony-demo"), "northlake")
    assert _flat(northlake.policy.sharing_description) == (
        "Our external academic price list, what is open on the public instrument "
        "calendar, sample-prep and air-free handling requirements, and typical "
        "turnaround. Not: other clients or their bookings, internal reserve slots, or "
        "discounts."
    )


def test_the_trusted_organization_example_leaves_the_story_to_the_sharing_check() -> None:
    """Northlake trusts Ridgeline with its holder specifications, in its default mode.
    Henderson is trusted by no one, so its consults meet each org's default."""
    scenario = load_scenario("harmony-demo")
    trusted = [(org.slug, row) for org in scenario.orgs for row in org.trusted_organizations]
    assert [(slug, row.org, row.autonomy) for slug, row in trusted] == [
        ("northlake", "ridgeline", None)
    ]
    ((_, row),) = trusted
    assert "reserve" not in row.sharing_description.lower()


def test_the_records_hold_what_the_story_turns_on() -> None:
    """The cassettes' answerers read these: Henderson has no seal material on file,
    Northlake's public calendar misses Nov 6 while Raj's note doesn't, and the list
    prices add up to the quotes ($1,410 and $325, $1,735 together)."""
    scenario = load_scenario("harmony-demo")
    henderson = " ".join(_records(scenario, "henderson").values()).lower()
    for word in ("seal", "o-ring", "gasket", "kalrez", "viton"):
        assert word not in henderson
    northlake = _records(scenario, "northlake")
    assert "XRD fully booked until Nov 10" in northlake["public-calendar"]
    assert "reserve XRD block on Wed Nov 4, 09:00–13:00" in northlake["reserve-xrd-block"]
    assert "Powder XRD $45 per sample. SEM/EDS $60 per sample." in northlake["price-list"]
    assert "Air-free transfer and handling $150 flat per batch." in northlake["price-list"]
    assert (
        "Academic users pay the external academic list price, priority slots included."
        in (northlake["pricing-policy"])
    )
    assert 12 * 45 + 12 * 60 + 150 == 1_410
    ridgeline = _records(scenario, "ridgeline")["price-list"]
    assert "$240, 4 business days" in ridgeline
    assert "Kalrez (+$85" in ridgeline
    assert 1_410 + 240 + 85 == 1_735
    budget = _records(scenario, "henderson")["budget"]
    assert "About $2,000 discretionary" in budget


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
    # The requester representative's whole mandate, as approved.
    assert request.brief == spec.brief
    assert brief_title(request.brief) == (
        "Rush SEM capacity for a battery OEM client, Thursday afternoons preferred"
    )
    (recipient,) = recipients_of(request)
    assert recipient.org_id == seed_id(scenario.name, "org", "northlake")
    # Its thread's cassette key, which story_adversary.yaml plays.
    assert keys.thread_key("vantage", "northlake") == "vantage.northlake"
