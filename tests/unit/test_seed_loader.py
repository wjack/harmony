"""The scenario format, the loader's error messages, and seeding in one transaction."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from harmony.cli import app as cli
from harmony.config import get_settings
from harmony.db.models import (
    Base,
    Event,
    KnowledgeItem,
    Member,
    NetworkRequest,
    Org,
    RepPolicy,
    RepSettings,
    Thread,
    ThreadMessage,
    TrustedOrganization,
    World,
)
from harmony.seeds import loader
from harmony.seeds.dates import resolve_date
from harmony.seeds.ids import seed_id
from harmony.seeds.loader import ScenarioNotFound, load_scenario, load_world
from harmony.seeds.schema import OrgSpec, ScenarioError
from tests.conftest import ClonedDatabase

type Sessionmaker = async_sessionmaker[AsyncSession]

ANCHOR = date(2026, 10, 26)


# --- A tiny scenario on disk ------------------------------------------------------------


def _org(slug: str, member: str, **overrides: Any) -> dict[str, Any]:
    return {
        "slug": slug,
        "name": slug.title(),
        "kind": "lab",
        "members": [{"slug": member, "name": member.title(), "role": "admin"}],
        "settings": {"public_summary": f"{slug} does things.", "instructions": "Be brief."},
        "policy": {
            "sharing_description": "Our prices.",
            "routing_prompt": f"Everything goes to {member}.",
            "default_assignee": member,
        },
        "knowledge": [
            {"slug": "prices", "title": "Prices", "body": "$1"},
            {"slug": "secret", "title": "Secret", "body": "shh"},
        ],
        **overrides,
    }


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def _tiny(
    root: Path,
    *,
    orgs: list[dict[str, Any]] | None = None,
    threads: list[dict[str, Any]] | None = None,
    requests: list[dict[str, Any]] | None = None,
    **scenario: Any,
) -> Path:
    """Write scenario `tiny` (orgs alpha/ann and beta/bob by default) under `root`."""
    orgs = orgs if orgs is not None else [_org("alpha", "ann"), _org("beta", "bob")]
    directory = root / "tiny"
    _write(
        directory / "scenario.yaml",
        {
            "name": "tiny",
            "anchor": ANCHOR,
            "default_member": "ann",
            "orgs": [org["slug"] for org in orgs],
            **scenario,
        },
    )
    for org in orgs:
        _write(directory / "orgs" / f"{org['slug']}.yaml", org)
    if threads is not None or requests is not None:
        _write(directory / "threads.yaml", {"threads": threads or [], "requests": requests or []})
    return directory


def _thread(**overrides: Any) -> dict[str, Any]:
    return {
        "slug": "t1",
        "requester": "alpha",
        "responder": "beta",
        "state": "closed",
        "request": {"brief": "Do a thing", "at": "-3d", "created_by": "ann"},
        "messages": [{"from": "alpha", "body": "Hello"}, {"from": "beta", "body": "Hi"}],
        "access": [{"member": "ann", "reason": "routed"}],
        **overrides,
    }


def _problem(root: Path) -> str:
    with pytest.raises(ScenarioError) as caught:
        load_scenario("tiny", root)
    return str(caught.value)


# --- Dates and IDs ----------------------------------------------------------------------


def test_relative_dates_resolve_against_the_anchor() -> None:
    assert resolve_date("+24d", ANCHOR) == date(2026, 11, 19)
    assert resolve_date("+11d", ANCHOR) == date(2026, 11, 6)
    assert resolve_date("-3d", ANCHOR) == date(2026, 10, 23)
    assert resolve_date("+0d", ANCHOR) == ANCHOR
    assert resolve_date(date(2026, 1, 2), ANCHOR) == date(2026, 1, 2)
    for bad in ("24d", "+1w", "tomorrow", "+ 3d", ""):
        with pytest.raises(ValueError, match="relative date"):
            resolve_date(bad, ANCHOR)
    with pytest.raises(ValueError, match="not a date and time"):
        resolve_date(datetime(2026, 1, 2, 3, 4), ANCHOR)  # noqa: DTZ001 - rejected either way


def test_seed_ids_are_deterministic_and_scoped_by_scenario() -> None:
    assert seed_id("harmony-demo", "member", "alex") == seed_id("harmony-demo", "member", "alex")
    assert seed_id("harmony-demo", "member", "alex") != seed_id("minimal", "member", "alex")
    assert seed_id("harmony-demo", "member", "alex") != seed_id("harmony-demo", "org", "alex")


# --- The demo scenario ------------------------------------------------------------------


def test_the_demo_scenario_is_structurally_complete() -> None:
    scenario = load_scenario("harmony-demo")
    orgs = {org.slug: org for org in scenario.orgs}
    # The story's cast and the adversary, then the ten background orgs (SCENARIO.md).
    featured = [org.slug for org in scenario.orgs if org.featured]
    assert featured == ["henderson", "northlake", "ridgeline", "calder", "vantage"]
    assert list(orgs)[: len(featured)] == featured
    assert len(orgs) == 15
    assert scenario.anchor == ANCHOR
    assert scenario.spec.default_member == "alex"

    def roster(org: OrgSpec) -> list[tuple[str, str, str]]:
        return [(m.slug, m.name, m.role) for m in org.members]

    assert roster(orgs["henderson"]) == [
        ("alex", "Alex Chen", "admin"),
        ("maya", "Prof. Maya Henderson", "admin"),
    ]
    assert roster(orgs["northlake"]) == [
        ("helen", "Dr. Helen Ward", "admin"),
        ("raj", "Raj Patel", "admin"),
    ]
    assert [m.slug for m in orgs["ridgeline"].members] == ["dana"]

    for org in scenario.orgs:
        assert org.knowledge, org.slug
        assert org.policy.routing_prompt
        assert org.settings.public_summary
        assert all(member.profile for member in org.members), org.slug
    for slug in ("northlake", "ridgeline", "calder"):
        assert any(item.kind == "price-list" for item in orgs[slug].knowledge), slug
    northlake = orgs["northlake"]
    assert northlake.policy.default_assignee == "helen"
    # The sharing description keeps the reserve block out (SCENARIO.md); Ridgeline is
    # the trusted organization example.
    assert "internal reserve slots" in northlake.policy.sharing_description
    assert [row.org for row in northlake.trusted_organizations] == ["ridgeline"]

    # The story depends on Henderson's knowledge never naming a seal material.
    henderson_text = " ".join(item.body.lower() for item in orgs["henderson"].knowledge)
    for word in ("seal", "gasket", "o-ring", "viton", "kalrez"):
        assert word not in henderson_text


def test_everyone_in_the_demo_is_an_admin() -> None:
    """The demo makes everyone an admin (FEEDBACK_R2_PLAN E1); tests of the view-only
    paths demote someone (`tests/integration/_worlds.demote_to_member`)."""
    for org in load_scenario("harmony-demo").orgs:
        assert all(member.role == "admin" for member in org.members), org.slug


def test_the_demo_scenario_seeds_the_access_fixtures() -> None:
    threads = {thread.slug: thread for thread in load_scenario("harmony-demo").threads}
    history = threads["henderson-northlake-precursor-xrd"]
    assert (history.requester, history.responder, history.state) == (
        "henderson",
        "northlake",
        "closed",
    )
    assert {(a.member, a.reason) for a in history.access} == {
        ("alex", "routed"),
        ("helen", "routed"),
    }
    routed_to_raj = [t for t in threads.values() if {a.member for a in t.access} == {"raj"}]
    assert len(routed_to_raj) == 1
    assert {a.reason for a in routed_to_raj[0].access} == {"routed"}


# --- Errors name the file and the YAML path -------------------------------------------


def test_an_unknown_field_names_the_file_and_path(tmp_path: Path) -> None:
    beta = _org("beta", "bob")
    beta["knowledge"][1]["colour"] = "blue"
    _tiny(tmp_path, orgs=[_org("alpha", "ann"), beta])
    message = _problem(tmp_path)
    assert "orgs/beta.yaml: knowledge[1].colour: Extra inputs are not permitted" in message
    assert "alpha.yaml" not in message


def test_a_bad_mode_names_the_file_and_path(tmp_path: Path) -> None:
    alpha = _org("alpha", "ann")
    alpha["policy"]["autonomy"] = "sometimes"
    _tiny(tmp_path, orgs=[alpha, _org("beta", "bob")])
    message = _problem(tmp_path)
    assert "orgs/alpha.yaml: policy.autonomy:" in message
    assert "'ask', 'auto' or 'skip'" in message
    assert "(got 'sometimes')" in message


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param(
            {
                "orgs": [
                    _org("alpha", "ann", policy={"routing_prompt": "x", "default_assignee": "zed"})
                ]
            },
            "orgs/alpha.yaml: policy.default_assignee: no member 'zed'",
            id="default-assignee",
        ),
        pytest.param(
            {"orgs": [_org("alpha", "ann", trusted_organizations=[{"org": "alpha"}])]},
            "orgs/alpha.yaml: trusted_organizations[0].org: no other org 'alpha'",
            id="trusts-itself",
        ),
        pytest.param(
            {"orgs": [_org("alpha", "ann"), _org("beta", "ann")]},
            "orgs/beta.yaml: members[0].slug: member 'ann' already exists in org 'alpha'",
            id="member-slug-reused",
        ),
        pytest.param(
            {"default_member": "nobody"},
            "tiny/scenario.yaml: default_member: no member 'nobody'",
            id="default-member",
        ),
        pytest.param(
            {"surprise": True},
            "tiny/scenario.yaml: surprise: Extra inputs are not permitted",
            id="scenario-extra-field",
        ),
        pytest.param(
            {"threads": [_thread(messages=[{"from": "gamma", "body": "Hi"}])]},
            "tiny/threads.yaml: threads[0].messages[0].from: 'gamma' is not a party",
            id="thread-sender",
        ),
        pytest.param(
            {"threads": [_thread(access=[{"member": "ann", "reason": "owner"}])]},
            "tiny/threads.yaml: threads[0].access[0].reason:",
            id="thread-access-reason",
        ),
        pytest.param(
            {"threads": [_thread(request={"brief": "x", "at": "soon"})]},
            "tiny/threads.yaml: threads[0].request.at: expected a date",
            id="thread-date",
        ),
        pytest.param(
            {"threads": [_thread(request={"brief": "x", "at": "+3d"})]},
            "tiny/threads.yaml: threads[0].request.at: seeded activity cannot be after",
            id="activity-in-the-future",
        ),
        pytest.param(
            {
                "threads": [
                    _thread(
                        request={"brief": "x", "at": "-5d"},
                        messages=[{"from": "alpha", "body": "Hello", "at": "-6d"}],
                    )
                ]
            },
            "tiny/threads.yaml: threads[0].messages[0].at: earlier than the request",
            id="message-before-its-request",
        ),
    ],
)
def test_reference_and_format_errors_name_the_file_and_path(
    tmp_path: Path, change: dict[str, Any], expected: str
) -> None:
    _tiny(tmp_path, **change)
    assert expected in _problem(tmp_path)


def test_an_org_file_missing_from_the_list_is_an_error(tmp_path: Path) -> None:
    directory = _tiny(tmp_path)
    _write(directory / "orgs" / "stray.yaml", _org("stray", "sue"))
    assert "orgs/stray.yaml: (file): not listed in scenario.yaml orgs" in _problem(tmp_path)


def test_invalid_yaml_names_the_line(tmp_path: Path) -> None:
    directory = _tiny(tmp_path)
    (directory / "orgs" / "beta.yaml").write_text("slug: beta\nmembers: [unclosed\n")
    assert "orgs/beta.yaml: line 3: invalid YAML" in _problem(tmp_path)


@pytest.mark.parametrize("name", ["nope", "../tiny", "Tiny", ""])
def test_unknown_or_unsafe_scenario_names_are_not_found(tmp_path: Path, name: str) -> None:
    _tiny(tmp_path)
    with pytest.raises(ScenarioNotFound):
        load_scenario(name, tmp_path)


# --- Seeding --------------------------------------------------------------------------


async def _count(sm: Sessionmaker, table: str, world_id: UUID | None = None) -> int:
    where = "" if world_id is None else f" WHERE world_id = '{world_id}'"
    async with sm() as session:
        return int(await session.scalar(text(f"SELECT count(*) FROM {table}{where}")) or 0)


async def _world_ids(sm: Sessionmaker, world_id: UUID) -> dict[str, set[UUID]]:
    async with sm() as session:
        return {
            model.__tablename__: set(
                await session.scalars(select(model.id).where(model.world_id == world_id))
            )
            for model in (Org, Member, KnowledgeItem, Thread)
        }


async def test_seeding_twice_gives_two_worlds_with_identical_seed_ids(
    db_sessionmaker: Sessionmaker,
) -> None:
    first = await load_world(db_sessionmaker, "harmony-demo", build_sha="test")
    second = await load_world(db_sessionmaker, "harmony-demo", build_sha="test")
    assert first != second

    ids = await _world_ids(db_sessionmaker, first)
    assert ids == await _world_ids(db_sessionmaker, second)
    assert seed_id("harmony-demo", "member", "alex") in ids["members"]
    scenario = load_scenario("harmony-demo")
    assert len(ids["orgs"]) == len(scenario.orgs)
    assert len(ids["threads"]) == len(scenario.threads)

    for table in (t.name for t in Base.metadata.sorted_tables if "world_id" in t.c):
        assert await _count(db_sessionmaker, table, first) == await _count(
            db_sessionmaker, table, second
        ), table

    async with db_sessionmaker() as session:
        for world_id in (first, second):
            created = await session.scalar(select(Event).where(Event.world_id == world_id))
            assert created is not None
            assert (created.seq, created.type) == (1, "world.created")
            world = await session.get_one(World, world_id)
            assert (world.scenario, world.status, world.mode) == ("harmony-demo", "live", "live")


async def test_seeded_history_sits_before_the_anchor_on_the_world_clock(
    db_sessionmaker: Sessionmaker,
) -> None:
    """The world clock reads anchor + (t - world created); seeded threads' times must
    land on their `at` dates, before the demo opens."""
    world_id = await load_world(db_sessionmaker, "harmony-demo", build_sha="test")
    scenario = load_scenario("harmony-demo")
    async with db_sessionmaker() as session:
        world = await session.get_one(World, world_id)

        def on_world_clock(stamp: datetime) -> date:
            return scenario.anchor + (stamp - world.created_at)  # whole days here

        for spec in scenario.threads:
            thread = await session.get_one(
                Thread, (world_id, seed_id("harmony-demo", "thread", spec.slug))
            )
            request = await session.get_one(NetworkRequest, (world_id, thread.request_id))
            messages = list(
                await session.scalars(
                    select(ThreadMessage)
                    .where(ThreadMessage.world_id == world_id, ThreadMessage.thread_id == thread.id)
                    .order_by(ThreadMessage.seq)
                )
            )
            assert spec.request.at is not None
            assert request.brief == spec.request.brief
            opened = resolve_date(spec.request.at, scenario.anchor)
            assert on_world_clock(request.created_at) == on_world_clock(thread.created_at) == opened
            assert [on_world_clock(m.created_at) for m in messages] == [
                resolve_date(m.at, scenario.anchor) for m in spec.messages if m.at is not None
            ]
            assert thread.last_activity_at == messages[-1].created_at < world.created_at
            assert on_world_clock(thread.last_activity_at) < scenario.anchor


async def test_seeded_rows_carry_the_scenario_values(
    db_sessionmaker: Sessionmaker, tmp_path: Path
) -> None:
    # Alpha trusts beta, which is seeded after it.
    alpha = _org(
        "alpha",
        "ann",
        trusted_organizations=[
            {"org": "beta", "autonomy": "ask", "sharing_description": "Also: our notes."}
        ],
    )
    _tiny(
        tmp_path,
        orgs=[alpha, _org("beta", "bob")],
        threads=[_thread()],
        requests=[
            {
                "slug": "r1",
                "requester": "beta",
                "created_by": "bob",
                "brief": "Find a lab for 14 powders.",
                "recipients": [{"org": "alpha", "reason": "Runs XRD.", "fit": "strong"}],
            }
        ],
    )
    world_id = await load_world(db_sessionmaker, "tiny", build_sha="test", root=tmp_path)
    alpha_id, beta_id = seed_id("tiny", "org", "alpha"), seed_id("tiny", "org", "beta")
    async with db_sessionmaker() as session:
        ann = await session.get_one(Member, (world_id, seed_id("tiny", "member", "ann")))
        assert ann.role == "admin"
        thread = await session.get_one(Thread, (world_id, seed_id("tiny", "thread", "t1")))
        assert (thread.state, thread.turn_count) == ("closed", 2)
        seeded = await session.get_one(NetworkRequest, (world_id, thread.request_id))
        assert (seeded.brief, seeded.status) == ("Do a thing", "closed")
        open_request = await session.get_one(
            NetworkRequest, (world_id, seed_id("tiny", "network_request", "r1"))
        )
        assert (open_request.brief, open_request.status) == ("Find a lab for 14 powders.", "active")
        assert open_request.recipients == [
            {
                "org_id": str(alpha_id),
                "fit": "strong",
                "reason": "Runs XRD.",
                "ask": None,
                "title": None,
                "suggested": None,
            }
        ]
        # Untitled requests and threads fall back to their briefs and openings.
        assert (seeded.title, thread.title, open_request.title) == (None, None, None)
        settings = await session.get_one(RepSettings, (world_id, alpha_id, 1))
        assert (settings.public_summary, settings.instructions) == (
            "alpha does things.",
            "Be brief.",
        )
        policy = await session.get_one(RepPolicy, (world_id, alpha_id))
        assert (policy.autonomy, policy.sharing_description) == ("auto", "Our prices.")
        assert policy.default_assignee_id == ann.id
        trusted = await session.get_one(TrustedOrganization, (world_id, alpha_id, beta_id))
        assert (trusted.autonomy, trusted.sharing_description, trusted.version) == (
            "ask",
            "Also: our notes.",
            1,
        )
        assert await session.get(TrustedOrganization, (world_id, beta_id, alpha_id)) is None


async def test_seeded_titles_and_asks_are_stored(
    db_sessionmaker: Sessionmaker, tmp_path: Path
) -> None:
    titled = _thread(
        request={
            "brief": "Do a thing",
            "title": "XRD on 14 powders",
            "at": "-3d",
            "created_by": "ann",
        }
    )
    recipient = {
        "org": "alpha",
        "reason": "Runs XRD.",
        "fit": "strong",
        "title": "XRD for a battery program",
        "ask": "Can you run 14 powders by Nov 6?",
        "suggested": True,
    }
    _tiny(
        tmp_path,
        threads=[titled],
        requests=[
            {
                "slug": "r1",
                "requester": "beta",
                "created_by": "bob",
                "brief": "Find a lab for 14 powders. Up to $2,000.",
                "title": "Powder XRD, up to $2,000",
                "recipients": [recipient],
            }
        ],
    )
    world_id = await load_world(db_sessionmaker, "tiny", build_sha="test", root=tmp_path)
    async with db_sessionmaker() as session:
        thread = await session.get_one(Thread, (world_id, seed_id("tiny", "thread", "t1")))
        seeded = await session.get_one(NetworkRequest, (world_id, thread.request_id))
        # A seeded thread is the request's only one: both sides see the same title.
        assert (seeded.title, thread.title) == ("XRD on 14 powders", "XRD on 14 powders")
        open_request = await session.get_one(
            NetworkRequest, (world_id, seed_id("tiny", "network_request", "r1"))
        )
        assert open_request.title == "Powder XRD, up to $2,000"
        assert open_request.recipients == [
            {
                "org_id": str(seed_id("tiny", "org", "alpha")),
                "fit": "strong",
                "reason": "Runs XRD.",
                "ask": "Can you run 14 powders by Nov 6?",
                "title": "XRD for a battery program",
                "suggested": True,
            }
        ]


_SEEDED_TABLES = ("worlds", "world_event_seq", "events", "orgs", "members", "knowledge_items")


async def test_a_failure_in_one_org_leaves_no_rows_behind(
    db_sessionmaker: Sessionmaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tiny(tmp_path)
    real_org_rows = loader._org_rows  # pyright: ignore[reportPrivateUsage]
    seen: list[str] = []

    def failing_org_rows(scenario: loader.Scenario, org: OrgSpec, world_id: UUID) -> list[Base]:
        seen.append(org.slug)
        if org.slug == "beta":
            raise RuntimeError("beta exploded")
        return real_org_rows(scenario, org, world_id)

    monkeypatch.setattr(loader, "_org_rows", failing_org_rows)
    with pytest.raises(RuntimeError, match="beta exploded"):
        await load_world(db_sessionmaker, "tiny", build_sha="test", root=tmp_path)

    assert seen == ["alpha", "beta"]
    for table in _SEEDED_TABLES:
        assert await _count(db_sessionmaker, table) == 0, table


async def test_a_database_error_in_one_org_rolls_back_the_others(
    db_sessionmaker: Sessionmaker, tmp_path: Path
) -> None:
    # Postgres rejects NUL in text: a failure only the database can find, in org 2.
    beta = _org("beta", "bob")
    beta["knowledge"][0]["body"] = "price\x00list"
    _tiny(tmp_path, orgs=[_org("alpha", "ann"), beta])
    with pytest.raises(DBAPIError):
        await load_world(db_sessionmaker, "tiny", build_sha="test", root=tmp_path)
    for table in _SEEDED_TABLES:
        assert await _count(db_sessionmaker, table) == 0, table


def test_cli_prints_the_world_and_row_counts(
    database: ClonedDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", database.dsn)
    monkeypatch.setenv("DATABASE_DIRECT_URL", database.dsn)
    get_settings.cache_clear()
    try:
        assert get_settings().database_url == SecretStr(database.dsn)
        result = CliRunner().invoke(cli, ["seed", "--scenario", "minimal"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("world ")
    assert lines[0].endswith("(scenario minimal)")
    counts = dict(line.split() for line in lines[1:])
    assert counts["orgs"] == "1"
    assert counts["members"] == "1"
    assert counts["events"] == "1"


def test_cli_reports_scenario_errors() -> None:
    result = CliRunner().invoke(cli, ["seed", "--scenario", "no-such-scenario"])
    assert result.exit_code == 1
    assert "no scenario 'no-such-scenario'" in result.output
    assert "harmony-demo" in result.output


async def test_world_ids_are_kept_apart_by_world_id(db_sessionmaker: Sessionmaker) -> None:
    first = await load_world(db_sessionmaker, "minimal", build_sha="test")
    second = await load_world(db_sessionmaker, "minimal", build_sha="test")
    sam = seed_id("minimal", "member", "sam")
    async with db_sessionmaker() as session:
        rows = (
            await session.execute(
                select(Member.world_id, func.count())
                .where(Member.id == sam)
                .group_by(Member.world_id)
            )
        ).all()
    assert dict(rows) == {first: 1, second: 1}
