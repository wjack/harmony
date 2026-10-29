"""Visitor worlds: created lazily, forked on reset or a new build, retired behind (§11.1).

A visitor session holds a desk world and a network world (`lens`), each with its
acting member. Every change of a session's world goes through one transaction that
seeds the new world, repoints the session, marks the old world `retiring` and, for an
existing session, bumps the epoch and pokes the `session` channel so streams opened on
the old world close. The old world is then handed to the `Retirer`.

Nothing model-driven runs when a world is created (LIVE_DEMO_PLAN 3.3). A seeded open
request (the Vantage probe, in the demo scenario) starts the first time the session's
acting member belongs to its requester or a recipient: after act-as
(`start_requests_for`), and after a switch that keeps the acting member. A
`world_workflows` row for the request's workflow means it started, so it starts at
most once per world. With live models, it waits while live calls can't run (paused,
or a provider block): its threads' first turns would be refused and end for good. The
next act-as or switch once they can run starts it.

While the passcode gate is on, the caller passes its `CreationLimits`: wiped-world
re-creates and Resets are counted per session before anything is seeded. New sessions
are not limited.

Worlds are never truncated in place: forking avoids racing workers that are mid-turn in
the old world (§11.1).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute

from harmony.db.models import Member, VisitorSession, World, WorldWorkflow
from harmony.db.models.choices import WorldStatus
from harmony.db.uow import unit_of_work
from harmony.events.bus import SESSION_CHANNEL
from harmony.llm.live_switch import live_report
from harmony.seeds.ids import seed_id
from harmony.seeds.loader import (
    DEFAULT_SCENARIO,
    SCENARIOS_DIR,
    Scenario,
    ScenarioNotFound,
    load_scenario,
    seed_world,
)
from harmony.seeds.schema import OpenRequestSpec, ScenarioError
from harmony.worlds.retirer import Retirer

log = logging.getLogger(__name__)

WorldLens = Literal["desk", "network"]


class RequestStarter(Protocol):
    async def start(self, world_id: UUID, request_ids: Sequence[UUID]) -> None:
        """Start the workflows of these seeded open requests of `world_id` (idempotent)."""
        ...


class TooManyWorlds(Exception):
    """A world-creation limit refused (429 `too_many_attempts`)."""

    def __init__(self, retry_after_s: int) -> None:
        super().__init__(f"too many new worlds; retry in {retry_after_s}s")
        self.retry_after_s = retry_after_s


class CreationLimits(Protocol):
    """World-creation limits while the passcode gate is on (`api.gate.WorldLimits`)."""

    def recreate(self, session_id: UUID) -> None:
        """Count a Reset or a wiped-world re-create of `session_id`'s world, or raise
        `TooManyWorlds`."""
        ...


class SessionNotFound(LookupError):
    def __init__(self, session_id: UUID) -> None:
        super().__init__(f"no session {session_id}")
        self.session_id = session_id


class NoWorld(LookupError):
    """The session has no live world for this lens; `start` creates one."""


@dataclass(frozen=True, slots=True)
class WorldSwitch:
    """A session moved to a new world. `previous_world_id` is now retiring or retired."""

    session_id: UUID
    lens: WorldLens
    world_id: UUID
    previous_world_id: UUID | None


@dataclass(frozen=True, slots=True)
class _LensState:
    world_id: UUID | None
    status: WorldStatus | None
    build_sha: str | None
    scenario: str | None


class _Any:
    """`expect` sentinel: switch whatever world the session points at."""


_ANY: Final = _Any()


def world_column(lens: WorldLens) -> InstrumentedAttribute[UUID | None]:
    """The `sessions` column that stores the session's world for `lens`."""
    return VisitorSession.desk_world_id if lens == "desk" else VisitorSession.network_world_id


def member_column(lens: WorldLens) -> InstrumentedAttribute[UUID | None]:
    """The `sessions` column that stores the session's acting member for `lens`."""
    return VisitorSession.desk_member_id if lens == "desk" else VisitorSession.network_member_id


class WorldService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        build_sha: str,
        retirer: Retirer,
        requests: RequestStarter | None = None,
        live_models: bool = False,
        scenarios_dir: Path = SCENARIOS_DIR,
        default_scenario: str = DEFAULT_SCENARIO,
    ) -> None:
        """Without `requests` (no Temporal), seeded open requests stay unstarted. With
        `live_models`, they wait while live calls can't run (the live switch)."""
        self._sessionmaker = sessionmaker
        self._build_sha = build_sha
        self._retirer = retirer
        self._requests = requests
        self._live_models = live_models
        self._scenarios_dir = scenarios_dir
        self._default_scenario = default_scenario

    async def start(
        self,
        session_id: UUID | None,
        *,
        lens: WorldLens = "desk",
        scenario: str | None = None,
        limits: CreationLimits | None = None,
    ) -> UUID:
        """Make sure the session (new if `session_id` is None or unknown) has a live,
        current-build world for `lens`; returns the session ID.

        The only place a world is created for a visitor who has none, so a page view
        never creates one. `scenario` applies only when a world is created; a session
        that already has a live world keeps it. Raises `ScenarioNotFound` or
        `ScenarioError` only for a bad `scenario` argument: a scenario stored on the
        session's old world that this build no longer has falls back to the default.

        With `limits`, a wiped world's re-create is counted (a fork for a new build is
        not), and `TooManyWorlds` is raised over the limit.
        """
        if session_id is not None:
            state = await self._lens_state(session_id, lens)
            if state is not None:
                if state.world_id is not None and state.status == "live":
                    if state.build_sha != self._build_sha:
                        await self._switch(
                            session_id, lens, state.scenario, expect=state.world_id, stored=True
                        )
                    return session_id
                # The world was wiped or never created: a fresh one.
                admit = (lambda: limits.recreate(session_id)) if limits is not None else None
                if scenario is not None:
                    await self._switch(
                        session_id, lens, scenario, expect=state.world_id, admit=admit
                    )
                else:
                    await self._switch(
                        session_id,
                        lens,
                        state.scenario,
                        expect=state.world_id,
                        stored=True,
                        admit=admit,
                    )
                return session_id
        switch = await self._switch(
            None,
            lens,
            scenario or self._default_scenario,
            expect=_ANY,
        )
        assert switch is not None
        return switch.session_id

    async def fork_and_reset(
        self,
        session_id: UUID,
        lens: WorldLens,
        *,
        observed_world_id: UUID,
        limits: CreationLimits | None = None,
    ) -> WorldSwitch:
        """Fork the session's `lens` world into a fresh one from the same scenario.

        `observed_world_id` is the world the caller acted in. If the session moved on
        concurrently (a double-clicked Reset), nothing is forked twice: the result
        reports the world the session is in now. With `limits`, a fork is counted as a
        re-create and `TooManyWorlds` is raised over the limit.
        """
        state = await self._lens_state(session_id, lens)
        if state is None:
            raise SessionNotFound(session_id)
        if state.world_id is None or state.status != "live":
            raise NoWorld(f"session {session_id} has no live {lens} world")
        if state.world_id == observed_world_id:
            switch = await self._switch(
                session_id,
                lens,
                state.scenario,
                expect=observed_world_id,
                stored=True,
                keep_member=False,
                admit=(lambda: limits.recreate(session_id)) if limits is not None else None,
            )
            if switch is not None:
                return switch
            state = await self._lens_state(session_id, lens)
        current = state.world_id if state is not None else None
        if current is None:
            raise NoWorld(f"session {session_id} has no live {lens} world")
        return WorldSwitch(session_id, lens, current, previous_world_id=observed_world_id)

    async def ensure_current_build(self, session_id: UUID, lens: WorldLens) -> WorldSwitch | None:
        """Fork the session's `lens` world if it was created under another build (§7).

        Deploys therefore never replay live workflows on changed code. Returns the
        switch, or None when the world is current (or there is none).
        """
        state = await self._lens_state(session_id, lens)
        if (
            state is None
            or state.world_id is None
            or state.status != "live"
            or state.build_sha == self._build_sha
        ):
            return None
        return await self._switch(
            session_id, lens, state.scenario, expect=state.world_id, stored=True
        )

    async def start_requests_for(self, world_id: UUID, org_id: UUID) -> None:
        """Start the seeded open requests of `world_id` that `org_id` is party to, as
        requester or recipient, and that haven't started: after the session acts as one
        of its members (3.3).

        Never raises: the visitor's world works without them; only their
        conversations are missing from it.
        """
        if self._requests is None:
            return
        try:
            async with self._sessionmaker() as session:
                name = await session.scalar(select(World.scenario).where(World.id == world_id))
            if name is None:
                return
            scenario = load_scenario(name, self._scenarios_dir)
        except Exception:
            log.exception("finding the seeded requests of world %s failed", world_id)
            return
        await self._start_party_requests(world_id, scenario, org_id)

    async def retire_all(self) -> list[UUID]:
        """Retire every world that isn't retired yet, one world at a time (the admin
        wipe, §7). Returns their IDs; a world whose retirement fails stays `retiring`."""
        async with self._sessionmaker() as session, session.begin():
            await session.execute(
                update(World).where(World.status == "live").values(status="retiring")
            )
            world_ids = list(
                await session.scalars(
                    select(World.id).where(World.status == "retiring").order_by(World.created_at)
                )
            )
        for world_id in world_ids:
            await self._retire(world_id)
        return world_ids

    # --- internals ---------------------------------------------------------------------

    async def _lens_state(self, session_id: UUID, lens: WorldLens) -> _LensState | None:
        """The session's world for `lens`, or None if the session doesn't exist."""
        statement = (
            select(VisitorSession.id, World.id, World.status, World.build_sha, World.scenario)
            .select_from(VisitorSession)
            .outerjoin(World, World.id == world_column(lens))
            .where(VisitorSession.id == session_id)
        )
        async with self._sessionmaker() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        _, world_id, status, build_sha, scenario = row
        return _LensState(world_id, status, build_sha, scenario)

    async def _switch(
        self,
        session_id: UUID | None,
        lens: WorldLens,
        scenario_name: str | None,
        *,
        expect: UUID | _Any | None,
        stored: bool = False,
        keep_member: bool = True,
        admit: Callable[[], None] | None = None,
    ) -> WorldSwitch | None:
        """Seed a new world and point the session's `lens` at it, in one transaction.

        `session_id=None` creates the session too. With
        `expect`, nothing happens (None is returned) unless the session still points
        at that world: two requests racing to fork the same world fork it once.
        `stored` says the name came from the session's old world rather than from the
        caller (see `_scenario`). `admit` counts the creation against a limit once the
        switch is sure to go ahead, and may raise `TooManyWorlds`.

        A fork for a new build keeps the acting member, and then starts the seeded
        requests that member's org is party to; a Reset (`keep_member=False`) starts
        over as the story's member, where the story begins.
        """
        scenario = self._scenario(scenario_name, stored=stored)
        world_id = uuid4()
        async with unit_of_work(self._sessionmaker, world_id) as uow:
            session = uow.session
            visitor: VisitorSession | None = None
            if session_id is not None:
                # Serializes act-as and other switches of this session.
                visitor = await session.get(
                    VisitorSession, session_id, with_for_update=True, populate_existing=True
                )
                if visitor is None:
                    raise SessionNotFound(session_id)
            previous = _world_of(visitor, lens) if visitor is not None else None
            if not isinstance(expect, _Any) and previous != expect:
                return None
            if admit is not None:
                admit()
            keep_slug = (
                await _member_slug(session, visitor, lens) if visitor and keep_member else None
            )
            start_slug = keep_slug or (scenario.story.member if scenario.story else None)

            seeded = await seed_world(uow, scenario, build_sha=self._build_sha)
            member_id = seeded.member_ids.get(start_slug or "", seeded.default_member_id)
            if visitor is None:
                visitor = VisitorSession(id=uuid4(), epoch=0)
                session.add(visitor)
            else:
                # Streams opened on the old world close on the epoch change.
                visitor.epoch += 1
                await session.execute(select(func.pg_notify(SESSION_CHANNEL, str(visitor.id))))
            _point(visitor, lens, world_id, member_id)
            visitor.last_seen_at = datetime.now(UTC)
            if previous is not None:
                await session.execute(
                    update(World)
                    .where(World.id == previous, World.status == "live")
                    .values(status="retiring")
                )
            await uow.commit()
            new_session_id = visitor.id

        kept_org = _org_of(scenario, keep_slug) if keep_slug is not None else None
        if kept_org is not None:
            await self._start_party_requests(world_id, scenario, kept_org)
        if previous is not None:
            await self._retire(previous)
        return WorldSwitch(new_session_id, lens, world_id, previous)

    def _scenario(self, name: str | None, *, stored: bool) -> Scenario:
        """Load `name` (the default when None).

        A `stored` name, read off the session's old world, may be one a later build
        renamed or removed. Failing on it would leave the session unable to fork, reset
        or start again, so it falls back to the default scenario; `_switch` keeps the
        acting member's slug where it still exists. A name the caller
        passed is never replaced: its error reaches the caller (a 400).
        """
        requested = name or self._default_scenario
        try:
            return load_scenario(requested, self._scenarios_dir)
        except (ScenarioNotFound, ScenarioError):
            if not stored or requested == self._default_scenario:
                raise
            log.warning(
                "scenario %r no longer loads in this build; forking into %r instead",
                requested,
                self._default_scenario,
                exc_info=True,
            )
            return load_scenario(self._default_scenario, self._scenarios_dir)

    async def _start_party_requests(self, world_id: UUID, scenario: Scenario, org_id: UUID) -> None:
        """Start the seeded requests `org_id` is party to that have no `world_workflows`
        row yet, one caller per world at a time, so a double click starts each once.
        While live calls can't run, nothing starts and nothing is recorded, so a later
        act-as or switch starts them."""
        if self._requests is None:
            return
        request_ids = _party_requests(scenario, org_id)
        if not request_ids:
            return
        # Imported here: payloads imports `agents.models`, then `auth.views`, then this.
        from harmony.temporal.workflows.payloads import request_workflow_id

        by_workflow = {request_workflow_id(world_id, rid): rid for rid in request_ids}
        try:
            if self._live_models:
                stopped_by = (await live_report(self._sessionmaker)).stopped_by
                if stopped_by is not None:
                    log.info(
                        "seeded requests of world %s wait: live calls stopped by %s",
                        world_id,
                        stopped_by,
                    )
                    return
            async with self._sessionmaker() as session, session.begin():
                # Held until the starts are recorded: only other starts in this world wait.
                key = f"seeded_requests:{world_id}"
                await session.execute(
                    select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
                )
                started = set(
                    await session.scalars(
                        select(WorldWorkflow.workflow_id).where(
                            WorldWorkflow.world_id == world_id,
                            WorldWorkflow.workflow_id.in_(by_workflow),
                        )
                    )
                )
                missing = [rid for wid, rid in by_workflow.items() if wid not in started]
                if missing:
                    await self._requests.start(world_id, missing)
        except Exception:
            # The visitor's world works without them; only their conversations are
            # missing from it.
            log.exception("starting the seeded requests of world %s failed", world_id)

    async def _retire(self, world_id: UUID) -> None:
        try:
            await self._retirer.retire(world_id)
        except Exception:
            # The visitor already has their new world; the old one stays `retiring`
            # for the startup sweep or the next wipe to finish.
            log.exception("retiring world %s failed", world_id)


def _world_of(visitor: VisitorSession, lens: WorldLens) -> UUID | None:
    return visitor.desk_world_id if lens == "desk" else visitor.network_world_id


def _org_of(scenario: Scenario, member_slug: str) -> UUID | None:
    for org in scenario.orgs:
        if any(member.slug == member_slug for member in org.members):
            return seed_id(scenario.name, "org", org.slug)
    return None


def _party_requests(scenario: Scenario, org_id: UUID) -> list[UUID]:
    """The scenario's open requests whose requester or a recipient is `org_id`."""
    name = scenario.name

    def parties(spec: OpenRequestSpec) -> set[UUID]:
        slugs = (spec.requester, *(recipient.org for recipient in spec.recipients))
        return {seed_id(name, "org", slug) for slug in slugs}

    return [
        seed_id(name, "network_request", spec.slug)
        for spec in scenario.requests
        if org_id in parties(spec)
    ]


def _point(visitor: VisitorSession, lens: WorldLens, world_id: UUID, member_id: UUID) -> None:
    if lens == "desk":
        visitor.desk_world_id, visitor.desk_member_id = world_id, member_id
    else:
        visitor.network_world_id, visitor.network_member_id = world_id, member_id


async def _member_slug(
    session: AsyncSession, visitor: VisitorSession, lens: WorldLens
) -> str | None:
    """The slug of the session's acting member in `lens`, if that member still exists."""
    world_id = _world_of(visitor, lens)
    member_id = visitor.desk_member_id if lens == "desk" else visitor.network_member_id
    if world_id is None or member_id is None:
        return None
    return await session.scalar(
        select(Member.slug).where(Member.world_id == world_id, Member.id == member_id)
    )
