"""Load a scenario from `scenarios/<name>/` and seed it into a new world.

Seeding is one UnitOfWork: the world, its event counter, every org's rows, the seeded
threads and open requests are written in one transaction that ends with
`world.created`, so a failure anywhere (a bad file, a constraint, a crash mid-org)
leaves no rows behind. Starting the open requests' workflows is the caller's, once the
world is committed.
Each org is flushed as it is added, so its rows really reach the database inside that
transaction. Seed IDs are `uuid5` (`seeds.ids`): identical in every world, kept apart
by `world_id`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import yaml
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.config import REPO_ROOT
from harmony.db import repo
from harmony.db.models import (
    Base,
    Community,
    KnowledgeItem,
    Member,
    NetworkRequest,
    Org,
    OrgCommunity,
    RepPolicy,
    RepSettings,
    Thread,
    ThreadAccess,
    ThreadMessage,
    TrustedOrganization,
)
from harmony.db.models.choices import WorldMode
from harmony.db.uow import UnitOfWork, unit_of_work
from harmony.events.schemas import EventScope, WorldCreated
from harmony.seeds.dates import resolve_date
from harmony.seeds.ids import seed_id
from harmony.seeds.schema import (
    SLUG_PATTERN,
    OpenRequestSpec,
    OrgSpec,
    Problem,
    ScenarioError,
    ScenarioSpec,
    SourcedOrg,
    StorySpec,
    ThreadSpec,
    ThreadsSpec,
    check_references,
    problems_from,
)

SCENARIOS_DIR = REPO_ROOT / "scenarios"
DEFAULT_SCENARIO = "harmony-demo"
# Seeded settings are version 1; later edits add versions (§9).
_SETTINGS_VERSION = 1
_TERMINAL_STATES = frozenset({"closed", "expired"})


class ScenarioNotFound(LookupError):
    def __init__(self, name: str, root: Path) -> None:
        super().__init__(f"no scenario {name!r} in {root}")
        self.name = name


@dataclass(frozen=True, slots=True)
class Scenario:
    """A parsed scenario whose references all resolve."""

    name: str
    directory: Path
    spec: ScenarioSpec
    orgs: tuple[OrgSpec, ...]
    threads: tuple[ThreadSpec, ...]
    # The suggested story (`story.yaml`), if the scenario has one.
    story: StorySpec | None = None
    # threads.yaml's open requests, for the world to start once it is created.
    requests: tuple[OpenRequestSpec, ...] = ()

    @property
    def anchor(self) -> date:
        return self.spec.anchor


@dataclass(frozen=True, slots=True)
class SeededWorld:
    world_id: UUID
    scenario: str
    # Member slug -> ID; slugs are unique per world and stable across worlds.
    member_ids: Mapping[str, UUID]
    default_member_id: UUID


# --- Reading --------------------------------------------------------------------------


def available_scenarios(root: Path = SCENARIOS_DIR) -> list[str]:
    """Scenario names under `root`: directories with a `scenario.yaml`."""
    if not root.is_dir():
        return []
    return sorted(
        path.name
        for path in root.iterdir()
        if re.fullmatch(SLUG_PATTERN, path.name) and (path / "scenario.yaml").is_file()
    )


def load_scenario(name: str, root: Path = SCENARIOS_DIR) -> Scenario:
    """Parse and check `root/name`. Raises `ScenarioNotFound` or `ScenarioError`."""
    directory = root / name
    scenario_path = directory / "scenario.yaml"
    # The name may come from an API request: never let it leave `root`.
    if not re.fullmatch(SLUG_PATTERN, name) or not scenario_path.is_file():
        raise ScenarioNotFound(name, root)

    scenario_file = _display(scenario_path)
    spec, problems = _parse(ScenarioSpec, scenario_path)
    if spec is None:
        raise ScenarioError(problems)
    if spec.name != name:
        problems.append(Problem(scenario_file, "name", f"must match the directory, {name!r}"))

    orgs: list[SourcedOrg] = []
    for i, slug in enumerate(spec.orgs):
        org_path = directory / "orgs" / f"{slug}.yaml"
        if not org_path.is_file():
            problems.append(Problem(scenario_file, f"orgs[{i}]", f"no file orgs/{slug}.yaml"))
            continue
        org, org_problems = _parse(OrgSpec, org_path)
        problems += org_problems
        if org is not None:
            if org.slug != slug:
                problems.append(
                    Problem(_display(org_path), "slug", f"must match the file, {slug!r}")
                )
            orgs.append(SourcedOrg(_display(org_path), org))
    for path in sorted((directory / "orgs").glob("*.yaml")):
        if path.stem not in spec.orgs:
            problems.append(Problem(_display(path), "(file)", "not listed in scenario.yaml orgs"))

    threads_path = directory / "threads.yaml"
    threads_file: str | None = None
    threads: list[ThreadSpec] = []
    requests: list[OpenRequestSpec] = []
    if threads_path.exists():
        threads_file = _display(threads_path)
        parsed, thread_problems = _parse(ThreadsSpec, threads_path)
        problems += thread_problems
        if parsed is not None:
            threads, requests = parsed.threads, parsed.requests

    story_path = directory / "story.yaml"
    story_file: str | None = None
    story: StorySpec | None = None
    if story_path.exists():
        story_file = _display(story_path)
        story, story_problems = _parse(StorySpec, story_path)
        problems += story_problems

    if problems:
        raise ScenarioError(problems)
    problems = check_references(
        scenario_file,
        spec,
        orgs,
        threads_file,
        threads,
        story_file=story_file,
        story=story,
        requests=requests,
    )
    if problems:
        raise ScenarioError(problems)
    return Scenario(
        name=name,
        directory=directory,
        spec=spec,
        orgs=tuple(org.spec for org in orgs),
        threads=tuple(threads),
        story=story,
        requests=tuple(requests),
    )


def _parse[M: BaseModel](model: type[M], path: Path) -> tuple[M | None, list[Problem]]:
    file = _display(path)
    try:
        data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, [Problem(file, "(file)", f"cannot read: {exc.strerror or exc}")]
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}" if isinstance(mark, yaml.Mark) else "(file)"
        reason = getattr(exc, "problem", None) or "syntax error"
        return None, [Problem(file, where, f"invalid YAML: {reason}")]
    try:
        return model.model_validate(data), []
    except ValidationError as exc:
        return None, problems_from(file, exc)


def _display(path: Path) -> str:
    """The path as a user would type it: relative to the working directory if inside it."""
    try:
        return str(path.resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


# --- Seeding --------------------------------------------------------------------------


async def seed_world(
    uow: UnitOfWork,
    scenario: Scenario,
    *,
    build_sha: str,
    budget_lineage: UUID,
    mode: WorldMode = "live",
) -> SeededWorld:
    """Add `uow.world_id` as a new world seeded from `scenario`, and queue `world.created`.

    The caller commits; until then nothing is visible. Flushes as it goes.
    """
    session = uow.session
    world_id = uow.world_id
    repo.add_world(
        session,
        world_id=world_id,
        scenario=scenario.name,
        mode=mode,
        build_sha=build_sha,
        budget_lineage=budget_lineage,
    )
    session.add_all(_community_rows(scenario, world_id))
    await session.flush()
    for org in scenario.orgs:
        session.add_all(_org_rows(scenario, org, world_id))
        await session.flush()
    for org in scenario.orgs:
        session.add_all(_trusted_rows(scenario, org, world_id))
    # The transaction's start time: the world's `created_at`, which the world clock maps
    # to the anchor. Seeded history is placed relative to it.
    world_created: datetime = (await session.execute(select(func.now()))).scalar_one()
    for thread in scenario.threads:
        session.add_all(_thread_rows(scenario, thread, world_id, world_created))
    session.add_all(_open_request(scenario, request, world_id) for request in scenario.requests)
    await session.flush()
    uow.add_event(
        WorldCreated(scenario=scenario.name, mode=mode, build_sha=build_sha), EventScope.world()
    )
    members = {
        member.slug: seed_id(scenario.name, "member", member.slug)
        for org in scenario.orgs
        for member in org.members
    }
    return SeededWorld(
        world_id=world_id,
        scenario=scenario.name,
        member_ids=members,
        default_member_id=members[scenario.spec.default_member],
    )


async def load_world(
    sessionmaker: async_sessionmaker[AsyncSession],
    scenario: str | Scenario,
    *,
    build_sha: str,
    budget_lineage: UUID | None = None,
    world_id: UUID | None = None,
    root: Path = SCENARIOS_DIR,
) -> UUID:
    """Seed a new live world from `scenario` in one transaction; returns its ID.

    Not attached to any visitor session (the world service does that); used by
    `harmony seed` and tests. A new budget lineage is minted unless one is given.
    """
    if isinstance(scenario, str):
        scenario = load_scenario(scenario, root)
    world_id = world_id or uuid4()
    async with unit_of_work(sessionmaker, world_id) as uow:
        await seed_world(
            uow, scenario, build_sha=build_sha, budget_lineage=budget_lineage or uuid4()
        )
        await uow.commit()
    return world_id


def _community_rows(scenario: Scenario, world_id: UUID) -> list[Base]:
    return [
        Community(
            world_id=world_id,
            id=seed_id(scenario.name, "community", community.slug),
            slug=community.slug,
            name=community.name,
            kind=community.kind,
        )
        for community in scenario.spec.communities
    ]


def _org_rows(scenario: Scenario, org: OrgSpec, world_id: UUID) -> list[Base]:
    """Everything one org owns but its trusted organizations (`_trusted_rows`): the
    org, members, knowledge, settings, policy, circles."""
    name = scenario.name
    org_id = seed_id(name, "org", org.slug)

    def member_id(slug: str) -> UUID:
        return seed_id(name, "member", slug)

    rows: list[Base] = [
        Org(
            world_id=world_id,
            id=org_id,
            slug=org.slug,
            name=org.name,
            kind=org.kind,
            featured=org.featured,
            current_settings_version=_SETTINGS_VERSION,
        )
    ]
    rows += [
        Member(
            world_id=world_id,
            id=member_id(member.slug),
            org_id=org_id,
            slug=member.slug,
            name=member.name,
            title=member.title,
            role=member.role,
            profile=member.profile,
        )
        for member in org.members
    ]
    rows += [
        KnowledgeItem(
            world_id=world_id,
            id=seed_id(name, "knowledge_item", f"{org.slug}/{item.slug}"),
            org_id=org_id,
            slug=item.slug,
            kind=item.kind,
            title=item.title,
            body=item.body,
            author_member_id=member_id(item.author) if item.author else None,
        )
        for item in org.knowledge
    ]
    settings, policy = org.settings, org.policy
    rows.append(
        RepSettings(
            world_id=world_id,
            org_id=org_id,
            version=_SETTINGS_VERSION,
            public_summary=settings.public_summary,
            instructions=settings.instructions,
        )
    )
    rows.append(
        RepPolicy(
            world_id=world_id,
            org_id=org_id,
            autonomy=policy.autonomy,
            sharing_description=policy.sharing_description,
            directory_visibility=policy.directory_visibility,
            routing_prompt=policy.routing_prompt,
            default_assignee_id=member_id(policy.default_assignee),
        )
    )
    rows += [
        OrgCommunity(
            world_id=world_id,
            community_id=seed_id(name, "community", community),
            org_id=org_id,
        )
        for community in org.communities
    ]
    return rows


def _trusted_rows(scenario: Scenario, org: OrgSpec, world_id: UUID) -> list[Base]:
    """The org's trusted organizations; they reference other orgs, so they're added
    once every org is."""
    org_id = seed_id(scenario.name, "org", org.slug)
    return [
        TrustedOrganization(
            world_id=world_id,
            org_id=org_id,
            counterparty_org_id=seed_id(scenario.name, "org", trusted.org),
            autonomy=trusted.autonomy,
            sharing_description=trusted.sharing_description,
        )
        for trusted in org.trusted_organizations
    ]


def _thread_rows(
    scenario: Scenario, thread: ThreadSpec, world_id: UUID, world_created: datetime
) -> Sequence[Base]:
    name = scenario.name
    thread_id = seed_id(name, "thread", thread.slug)
    request_id = seed_id(name, "network_request", thread.slug)
    request = thread.request

    def wall_clock(value: date | str | None) -> datetime | None:
        """An anchor-relative seed date as the stored time that the world clock
        (anchor + (t - world created) + fast-forward) shows on that date."""
        if value is None:
            return None
        return world_created + (resolve_date(value, scenario.anchor) - scenario.anchor)

    # Unset times keep the column default: when the world is created.
    opened = wall_clock(request.at)
    sent: list[datetime | None] = []
    for message in thread.messages:
        sent.append(wall_clock(message.at) or (sent[-1] if sent else opened))
    last_activity = sent[-1] if sent else opened
    ended = thread.state in _TERMINAL_STATES

    def stamped(**times: datetime | None) -> dict[str, datetime]:
        return {column: value for column, value in times.items() if value is not None}

    def org_id(slug: str) -> UUID:
        return seed_id(name, "org", slug)

    def member_id(slug: str | None) -> UUID | None:
        return seed_id(name, "member", slug) if slug else None

    rows: list[Base] = [
        NetworkRequest(
            world_id=world_id,
            id=request_id,
            requester_org_id=org_id(thread.requester),
            created_by_member_id=member_id(request.created_by),
            title=request.title,
            brief=request.brief,
            status="closed" if ended else "active",
            **stamped(created_at=opened),
        ),
        Thread(
            world_id=world_id,
            id=thread_id,
            request_id=request_id,
            requester_org_id=org_id(thread.requester),
            responder_org_id=org_id(thread.responder),
            state=thread.state,
            side_to_move=thread.side_to_move,
            requester_settings_version=_SETTINGS_VERSION,
            responder_settings_version=_SETTINGS_VERSION,
            turn_count=len(thread.messages),
            title=request.title,
            # Seeded history ended as it reads; it has no `thread_runs`, so it is never
            # reopened (REOPEN.md D5).
            end_reason=("closed" if thread.state == "closed" else "quiet") if ended else None,
            ended_at=(last_activity or world_created) if ended else None,
            **stamped(created_at=opened, last_activity_at=last_activity),
        ),
    ]
    for seq, (message, at) in enumerate(zip(thread.messages, sent, strict=True), start=1):
        rows.append(
            ThreadMessage(
                world_id=world_id,
                id=seed_id(name, "thread_message", f"{thread.slug}/{seq}"),
                thread_id=thread_id,
                seq=seq,
                from_org_id=org_id(message.sender),
                kind=message.kind,
                body=message.body,
                provenance=message.provenance,
                author_member_id=member_id(message.member)
                if message.provenance == "human"
                else None,
                approved_by_member_id=member_id(message.member)
                if message.provenance == "agent_approved"
                else None,
                **stamped(created_at=at),
            )
        )
    rows += [
        ThreadAccess(
            world_id=world_id,
            thread_id=thread_id,
            member_id=member_id(access.member),
            reason=access.reason,
            shared_by_member_id=member_id(access.shared_by),
            question=access.question,
        )
        for access in thread.access
    ]
    return rows


def _open_request(scenario: Scenario, spec: OpenRequestSpec, world_id: UUID) -> NetworkRequest:
    """An open request as an approved outreach card leaves it (`domain.requests`): its
    brief and recipients, and no threads yet."""
    name = scenario.name
    return NetworkRequest(
        world_id=world_id,
        id=seed_id(name, "network_request", spec.slug),
        requester_org_id=seed_id(name, "org", spec.requester),
        created_by_member_id=seed_id(name, "member", spec.created_by),
        title=spec.title,
        brief=spec.brief,
        recipients=[
            {
                "org_id": str(seed_id(name, "org", recipient.org)),
                "fit": recipient.fit,
                "reason": recipient.reason,
                "ask": recipient.ask,
                "title": recipient.title,
                "suggested": recipient.suggested,
            }
            for recipient in spec.recipients
        ],
        status="active",
    )
