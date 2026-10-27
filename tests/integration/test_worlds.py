"""Sessions and worlds through the API: lazy creation, act-as, reset, build pinning,
the lazy start of seeded requests (LIVE_DEMO_PLAN 3.3) and the admin wipe
(ARCHITECTURE.md §7, §11.1, §11.3)."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from harmony.api.deps import AppServices, get_world_service
from harmony.auth.session import SESSION_COOKIE, sign_session_id
from harmony.config import Settings
from harmony.db.models import Event, Org, VisitorSession, World, WorldWorkflow
from harmony.events.bus import SESSION_CHANNEL
from harmony.llm.budget_pg import HostedCaps, set_live
from harmony.seeds.loader import DEFAULT_SCENARIO, SCENARIOS_DIR, load_scenario, load_world
from harmony.temporal.workflows.payloads import request_workflow_id
from harmony.worlds.retirer import SyncRetirer
from harmony.worlds.service import WorldService
from tests.conftest import ClonedDatabase
from tests.integration._visitors import (
    ADMIN_SECRET,
    build_app,
    cursor_of,
    demo_id,
    visitor,
)

type Sessionmaker = async_sessionmaker[AsyncSession]

# Every org the demo scenario seeds into a world.
DEMO_ORGS = len(load_scenario(DEFAULT_SCENARIO).orgs)


@pytest.fixture
def app(app_settings: Settings, db_engine: AsyncEngine) -> FastAPI:
    return build_app(app_settings, db_engine)


async def _worlds(sm: Sessionmaker) -> dict[UUID, World]:
    async with sm() as session:
        return {world.id: world for world in await session.scalars(select(World))}


async def _events(sm: Sessionmaker, world_id: UUID) -> list[Event]:
    async with sm() as session:
        return list(
            await session.scalars(
                select(Event).where(Event.world_id == world_id).order_by(Event.seq)
            )
        )


async def _org_count(sm: Sessionmaker, world_id: UUID) -> int:
    async with sm() as session:
        count = await session.scalar(
            select(func.count()).select_from(Org).where(Org.world_id == world_id)
        )
    return count or 0


async def _visitor_row(sm: Sessionmaker, session_id: str) -> VisitorSession:
    async with sm() as session:
        return await session.get_one(VisitorSession, UUID(session_id))


def _with_retirer(app: FastAPI, retirer: SyncRetirer) -> None:
    services: AppServices = app.state.services
    app.state.services = replace(services, retirer=retirer)


# --- Starting -------------------------------------------------------------------------


async def test_start_creates_the_session_and_its_world_lazily(
    app: FastAPI, app_settings: Settings, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        await v.request("GET", "/session", expect=401)
        await v.request("GET", "/members", expect=401)
        await v.request("GET", "/threads", expect=401)
        assert await _worlds(db_sessionmaker) == {}, "a page view must not create a world"

        response = await v.request("POST", "/session/start")
        cookie = response.headers["set-cookie"]
        assert cookie.startswith(f"{SESSION_COOKIE}=")
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert ("Secure" in cookie) == (app_settings.app_env != "local")
        started = response.json()
        assert started["lens"] == "desk"
        assert started["epoch"] == 0
        assert (started["member"]["slug"], started["member"]["org"]["slug"]) == (
            "alex",
            "henderson",
        )
        assert started["world"]["scenario"] == "harmony-demo"
        assert started["world"]["status"] == "live"
        assert cursor_of(response) == 1  # world.created

        current = await v.request("GET", "/session")
        assert current.json() == started
        assert cursor_of(current) == 1
        again = await v.start()
        assert (again["session_id"], again["world_id"]) == (
            started["session_id"],
            started["world_id"],
        )

    [world] = (await _worlds(db_sessionmaker)).values()
    assert str(world.id) == started["world_id"]
    assert world.build_sha == app_settings.build_sha
    row = await _visitor_row(db_sessionmaker, started["session_id"])
    assert row.budget_lineage == world.budget_lineage
    assert row.desk_member_id == demo_id("member", "alex")
    assert row.network_world_id is None


async def test_bad_or_unknown_cookies_are_unauthenticated(
    app: FastAPI, app_settings: Settings
) -> None:
    secret = app_settings.session_secret_value()
    for value in ("garbage", sign_session_id(uuid4(), "another-secret")):
        async with visitor(app, httpx.Cookies({SESSION_COOKIE: value})) as v:
            await v.request("GET", "/session", expect=401)
    unknown = sign_session_id(uuid4(), secret)
    async with visitor(app, httpx.Cookies({SESSION_COOKIE: unknown})) as v:
        await v.request("GET", "/session", expect=401)
        started = await v.start()
        assert started["session_id"] != unknown


async def test_start_with_an_unknown_scenario_is_refused(app: FastAPI) -> None:
    async with visitor(app) as v:
        detail = (await v.json("POST", "/session/start", json={"scenario": "nope"}, expect=400))[
            "detail"
        ]
        assert "harmony-demo" in detail
        await v.request("GET", "/session", expect=401)


# --- Act-as ---------------------------------------------------------------------------


async def test_act_as_bumps_the_epoch_emits_member_changed_and_notifies(
    app: FastAPI, database: ClonedDatabase, db_sessionmaker: Sessionmaker
) -> None:
    pokes: asyncio.Queue[str] = asyncio.Queue()

    def on_notify(_conn: object, _pid: int, _channel: str, payload: object) -> None:
        pokes.put_nowait(str(payload))

    listener = await asyncpg.connect(database.dsn)
    await listener.add_listener(SESSION_CHANNEL, on_notify)
    try:
        async with visitor(app) as v:
            started = await v.start()
            alex_inbox = await v.inbox()
            helen = (await v.member_ids())["helen"]
            response = await v.request("POST", "/session/act-as", json={"member_id": str(helen)})
            switched = response.json()
            assert switched["member"]["slug"] == "helen"
            assert switched["epoch"] == 1
            assert switched["world_id"] == started["world_id"]
            assert cursor_of(response) == 2
            assert await asyncio.wait_for(pokes.get(), timeout=5) == started["session_id"]
            assert (await v.session())["member"]["slug"] == "helen"
            assert await v.inbox() != alex_inbox
    finally:
        await listener.close()

    world_id = UUID(started["world_id"])
    [created, changed] = await _events(db_sessionmaker, world_id)
    assert created.type == "world.created"
    assert (changed.seq, changed.type, changed.scope_kind) == (
        2,
        "session.member_changed",
        "session",
    )
    assert changed.session_id == UUID(started["session_id"])
    assert changed.payload == {
        "type": "session.member_changed",
        "session_id": started["session_id"],
        "member_id": str(helen),
        "epoch": 1,
    }


async def test_act_as_refuses_a_member_of_another_world(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v, visitor(app) as other:
        started = await v.start()
        await other.start(scenario="minimal")
        sam = (await other.member_ids())["sam"]
        assert sam not in (await v.member_ids()).values()

        for member_id in (sam, uuid4()):
            await v.request(
                "POST", "/session/act-as", json={"member_id": str(member_id)}, expect=403
            )
        await v.request("POST", "/session/act-as", json={"member_id": "alex"}, expect=422)

        unchanged = await v.session()
        assert (unchanged["epoch"], unchanged["member"]["slug"]) == (0, "alex")
    assert [e.type for e in await _events(db_sessionmaker, UUID(started["world_id"]))] == [
        "world.created"
    ]


# --- Reset ----------------------------------------------------------------------------


async def test_reset_forks_a_fresh_world_and_retires_the_old_one(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        started = await v.start()
        before = await v.act_as("helen")
        response = await v.request("POST", "/worlds/reset")
        reset = response.json()
        after = reset["session"]

        old, new = UUID(started["world_id"]), UUID(after["world_id"])
        assert new != old
        assert reset["previous_world_id"] == str(old)
        assert reset["previous_world_status"] == "retired"
        assert after["session_id"] == started["session_id"]
        assert after["epoch"] == before["epoch"] + 1
        # Back at the start of the story: acting as its member again (seed IDs: the
        # same member ID as in the first world).
        assert after["member"]["slug"] == "alex"
        assert after["member"]["id"] == started["member"]["id"]
        assert before["member"]["slug"] == "helen"
        assert cursor_of(response) == 1
        assert (await v.session())["world_id"] == str(new)
        assert {"alex", "helen", "raj"} <= set(await v.member_ids())

    worlds = await _worlds(db_sessionmaker)
    assert set(worlds) == {old, new}
    assert worlds[old].status == "retired"
    assert worlds[old].retired_at is not None
    assert worlds[new].status == "live"
    assert await _org_count(db_sessionmaker, old) == 0, "SyncRetirer deletes the old rows"
    assert await _org_count(db_sessionmaker, new) == DEMO_ORGS


async def test_reset_carries_the_budget_lineage(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        started = await v.start()
        first = (await v.json("POST", "/worlds/reset"))["session"]
        second = (await v.json("POST", "/worlds/reset"))["session"]

    lineage = (await _visitor_row(db_sessionmaker, started["session_id"])).budget_lineage
    worlds = await _worlds(db_sessionmaker)
    world_ids = [UUID(s["world_id"]) for s in (started, first, second)]
    assert len(set(world_ids)) == 3
    assert {worlds[world_id].budget_lineage for world_id in world_ids} == {lineage}


async def test_a_retirement_that_keeps_rows_emits_world_retired(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    _with_retirer(app, SyncRetirer(db_sessionmaker, row_ttl_s=3600))
    async with visitor(app) as v:
        started = await v.start()
        reset = await v.json("POST", "/worlds/reset")
    old = UUID(started["world_id"])
    assert reset["previous_world_status"] == "retired"
    assert [e.type for e in await _events(db_sessionmaker, old)] == [
        "world.created",
        "world.retired",
    ]
    assert await _org_count(db_sessionmaker, old) == DEMO_ORGS


async def test_concurrent_resets_of_one_world_fork_it_once(
    app: FastAPI, app_settings: Settings, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        started = await v.start()
    service = WorldService(
        db_sessionmaker, build_sha=app_settings.build_sha, retirer=SyncRetirer(db_sessionmaker)
    )
    session_id, old = UUID(started["session_id"]), UUID(started["world_id"])
    switches = await asyncio.gather(
        *(service.fork_and_reset(session_id, "desk", observed_world_id=old) for _ in range(3))
    )
    assert len({switch.world_id for switch in switches}) == 1
    assert len(await _worlds(db_sessionmaker)) == 2


# --- Seeded open requests (3.3) ----------------------------------------------------------

# The Vantage probe of Northlake: requester Vantage (Jordan), recipient Northlake (Helen,
# Raj).
ADVERSARY = demo_id("network_request", "vantage-northlake-thursday-sem")


class _RecordingStarter:
    """Records each start, and its `world_workflows` row as `start_world_workflow` does."""

    def __init__(self, sm: Sessionmaker, *, fail: bool = False) -> None:
        self.started: list[tuple[UUID, tuple[UUID, ...]]] = []
        self._sm = sm
        self._fail = fail

    async def start(self, world_id: UUID, request_ids: Sequence[UUID]) -> None:
        self.started.append((world_id, tuple(request_ids)))
        if self._fail:
            raise RuntimeError("temporal is down")
        rows = [
            {
                "world_id": world_id,
                "workflow_id": request_workflow_id(world_id, request_id),
                "workflow_type": "NetworkRequestWorkflow",
            }
            for request_id in request_ids
        ]
        async with self._sm() as session, session.begin():
            await session.execute(insert(WorldWorkflow).values(rows).on_conflict_do_nothing())


def _starting(
    app: FastAPI,
    sm: Sessionmaker,
    starter: _RecordingStarter,
    *,
    build_sha: str,
    caps: HostedCaps | None = None,
) -> FastAPI:
    """`app` with `starter` in place of Temporal's, and `caps` as hosted live calls."""
    app.dependency_overrides[get_world_service] = lambda: WorldService(
        sm, build_sha=build_sha, retirer=SyncRetirer(sm), requests=starter, caps=caps
    )
    return app


# Hosted caps with only a lineage key, so no global row is needed.
LIVE_CAPS = HostedCaps(lineage_usd=Decimal(10))


async def test_creating_a_world_starts_no_seeded_request(
    app_settings: Settings, db_sessionmaker: Sessionmaker
) -> None:
    starter = _RecordingStarter(db_sessionmaker)
    service = WorldService(
        db_sessionmaker,
        build_sha=app_settings.build_sha,
        retirer=SyncRetirer(db_sessionmaker),
        requests=starter,
    )
    session_id = await service.start(None)
    first = (await _visitor_row(db_sessionmaker, str(session_id))).desk_world_id
    assert first is not None
    reset = await service.fork_and_reset(session_id, "desk", observed_world_id=first)
    # A fork for a new build keeps Alex, whose lab is party to no seeded request.
    deployed = WorldService(
        db_sessionmaker,
        build_sha="the-next-build",
        retirer=SyncRetirer(db_sessionmaker),
        requests=starter,
    )
    forked = await deployed.ensure_current_build(session_id, "desk")
    assert forked is not None
    assert forked.previous_world_id == reset.world_id
    await service.start(None, scenario="minimal")
    assert starter.started == []


async def test_the_first_act_as_into_a_party_org_starts_the_seeded_request(
    app: FastAPI, app_settings: Settings, db_sessionmaker: Sessionmaker
) -> None:
    starter = _RecordingStarter(db_sessionmaker)
    _starting(app, db_sessionmaker, starter, build_sha=app_settings.build_sha)
    async with visitor(app) as v:
        world = UUID((await v.start())["world_id"])
        await v.act_as("maya")
        assert starter.started == []
        await v.act_as("helen")
        assert starter.started == [(world, (ADVERSARY,))]
        # Once per world, however the visitor switches.
        await v.act_as("raj")
        await v.act_as("alex")
        await v.act_as("helen")
        # A Reset starts over as Alex, so it starts nothing either.
        await v.json("POST", "/worlds/reset")
    assert starter.started == [(world, (ADVERSARY,))]

    async with visitor(app) as requester:
        vantage_world = UUID((await requester.start())["world_id"])
        await requester.act_as("jordan")
    assert starter.started[1:] == [(vantage_world, (ADVERSARY,))]


async def test_a_double_clicked_act_as_starts_it_once(
    app: FastAPI, app_settings: Settings, db_sessionmaker: Sessionmaker
) -> None:
    starter = _RecordingStarter(db_sessionmaker)
    _starting(app, db_sessionmaker, starter, build_sha=app_settings.build_sha)
    async with visitor(app) as v:
        world = UUID((await v.start())["world_id"])
        helen = (await v.member_ids())["helen"]
        await asyncio.gather(
            *(v.json("POST", "/session/act-as", json={"member_id": str(helen)}) for _ in range(3))
        )
    assert starter.started == [(world, (ADVERSARY,))]


async def test_a_fork_that_keeps_a_party_member_starts_it_in_the_new_world(
    app: FastAPI, app_settings: Settings, db_engine: AsyncEngine, db_sessionmaker: Sessionmaker
) -> None:
    starter = _RecordingStarter(db_sessionmaker)
    _starting(app, db_sessionmaker, starter, build_sha=app_settings.build_sha)
    async with visitor(app) as v:
        old = UUID((await v.start())["world_id"])
        await v.act_as("helen")
        cookies = v.cookies()

    deployed = _starting(
        build_app(app_settings, db_engine, build_sha="the-next-build"),
        db_sessionmaker,
        starter,
        build_sha="the-next-build",
    )
    async with visitor(deployed, cookies) as v:
        current = await v.session()
        assert current["member"]["slug"] == "helen"
        await v.session()
        await v.act_as("helen")
    new = UUID(current["world_id"])
    assert new != old
    assert starter.started == [(old, (ADVERSARY,)), (new, (ADVERSARY,))]


async def test_a_seeded_request_waits_while_live_calls_cant_run(
    app: FastAPI, app_settings: Settings, db_sessionmaker: Sessionmaker
) -> None:
    starter = _RecordingStarter(db_sessionmaker)
    _starting(app, db_sessionmaker, starter, build_sha=app_settings.build_sha, caps=LIVE_CAPS)
    await set_live(db_sessionmaker, enabled=False)
    async with visitor(app) as v:
        world = UUID((await v.start())["world_id"])
        await v.act_as("helen")
        # Its threads' first turns would be refused and end for good: nothing starts,
        # and nothing is recorded that would stop a later start.
        assert starter.started == []
        await set_live(db_sessionmaker, enabled=True)
        await v.act_as("raj")
        await v.act_as("helen")
    assert starter.started == [(world, (ADVERSARY,))]


async def test_a_fork_while_live_calls_cant_run_starts_it_once_they_can(
    app: FastAPI, app_settings: Settings, db_engine: AsyncEngine, db_sessionmaker: Sessionmaker
) -> None:
    starter = _RecordingStarter(db_sessionmaker)
    _starting(app, db_sessionmaker, starter, build_sha=app_settings.build_sha, caps=LIVE_CAPS)
    await set_live(db_sessionmaker, enabled=False)
    async with visitor(app) as v:
        await v.start()
        await v.act_as("helen")
        cookies = v.cookies()

    deployed = _starting(
        build_app(app_settings, db_engine, build_sha="the-next-build"),
        db_sessionmaker,
        starter,
        build_sha="the-next-build",
        caps=LIVE_CAPS,
    )
    async with visitor(deployed, cookies) as v:
        # The fork keeps Helen, while live models are still paused.
        current = await v.session()
        assert current["member"]["slug"] == "helen"
        assert starter.started == []
        await set_live(db_sessionmaker, enabled=True)
        await v.act_as("helen")
    assert starter.started == [(UUID(current["world_id"]), (ADVERSARY,))]


async def test_a_failed_request_start_still_lets_the_visitor_act(
    app: FastAPI, app_settings: Settings, db_sessionmaker: Sessionmaker
) -> None:
    starter = _RecordingStarter(db_sessionmaker, fail=True)
    _starting(app, db_sessionmaker, starter, build_sha=app_settings.build_sha)
    async with visitor(app) as v:
        world = UUID((await v.start())["world_id"])
        switched = await v.act_as("helen")
        assert switched["member"]["slug"] == "helen"
        # Nothing was recorded, so the next act-as tries again.
        await v.act_as("raj")
    assert starter.started == [(world, (ADVERSARY,)), (world, (ADVERSARY,))]
    assert (await _worlds(db_sessionmaker))[world].status == "live"


async def test_a_world_from_another_build_is_forked_on_the_next_visit(
    app: FastAPI, app_settings: Settings, db_engine: AsyncEngine, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        started = await v.start()
        await v.act_as("raj")
        cookies = v.cookies()

    deployed = build_app(app_settings, db_engine, build_sha="the-next-build")
    async with visitor(deployed, cookies) as v:
        current = await v.session()
    assert current["world_id"] != started["world_id"]
    assert current["member"]["slug"] == "raj"
    worlds = await _worlds(db_sessionmaker)
    assert worlds[UUID(current["world_id"])].build_sha == "the-next-build"
    assert worlds[UUID(started["world_id"])].status == "retired"


async def test_a_world_whose_scenario_a_later_build_removed_forks_into_the_default(
    app_settings: Settings,
    db_engine: AsyncEngine,
    db_sessionmaker: Sessionmaker,
    tmp_path: Path,
) -> None:
    # Build A had a scenario "ghost"; build B (the app below) has no such directory.
    ghost = tmp_path / "ghost"
    shutil.copytree(SCENARIOS_DIR / "minimal", ghost)
    spec = ghost / "scenario.yaml"
    spec.write_text(spec.read_text().replace("name: minimal", "name: ghost"))
    build_a = WorldService(
        db_sessionmaker,
        build_sha="build-a",
        retirer=SyncRetirer(db_sessionmaker),
        scenarios_dir=tmp_path,
    )
    session_id = await build_a.start(None, scenario="ghost")
    lineage = (await _visitor_row(db_sessionmaker, str(session_id))).budget_lineage
    cookies = httpx.Cookies()
    cookies.set(SESSION_COOKIE, sign_session_id(session_id, app_settings.session_secret_value()))

    deployed = build_app(app_settings, db_engine, build_sha="build-b")
    async with visitor(deployed, cookies) as v:
        # Every authenticated request used to 500 here, and start to 400.
        current = await v.session()
        await v.inbox()
        restarted = await v.start()
        assert restarted["world_id"] == current["world_id"]
        reset = await v.json("POST", "/worlds/reset")
        # A live world ignores a named scenario; a new session still checks it (below).
        named = await v.request("POST", "/session/start", json={"scenario": "ghost"}, expect=200)
        assert named.json()["world_id"] == reset["session"]["world_id"]

    assert current["session_id"] == str(session_id)
    worlds = await _worlds(db_sessionmaker)
    forked = worlds[UUID(current["world_id"])]
    assert (forked.scenario, forked.build_sha) == ("harmony-demo", "build-b")
    assert {world.budget_lineage for world in worlds.values()} == {lineage}
    assert {w.status for w in worlds.values() if w.scenario == "ghost"} == {"retired"}

    async with visitor(deployed) as stranger:
        refused = await stranger.request(
            "POST", "/session/start", json={"scenario": "ghost"}, expect=400
        )
    assert "unknown scenario 'ghost'" in refused.json()["detail"]


# --- The network lens -----------------------------------------------------------------


async def test_the_network_world_is_not_started_until_p3(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        refused = await v.request("POST", "/session/start", params={"world": "network"}, expect=404)
        assert refused.json()["code"] == "not_found"
        assert await _worlds(db_sessionmaker) == {}
        desk = await v.start()
        await v.request("POST", "/session/start", params={"world": "network"}, expect=404)
        await v.request("GET", "/session", params={"world": "network"}, expect=401)
    row = await _visitor_row(db_sessionmaker, desk["session_id"])
    assert row.network_world_id is None


# --- Admin wipe -----------------------------------------------------------------------


async def test_the_admin_wipe_needs_the_secret_and_retires_every_world(
    app: FastAPI, app_settings: Settings, db_engine: AsyncEngine, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v, visitor(app) as other:
        first = await v.start()
        await other.start()

        refused = await v.request("POST", "/admin/wipe", expect=403)
        assert refused.json()["code"] == "not_admin"
        await v.request("POST", "/admin/wipe", headers={"X-Admin-Secret": "wrong"}, expect=403)
        unset = build_app(app_settings, db_engine, admin_secret=None)
        async with visitor(unset) as anyone:
            await anyone.request(
                "POST", "/admin/wipe", headers={"X-Admin-Secret": ADMIN_SECRET}, expect=403
            )
        assert all(w.status == "live" for w in (await _worlds(db_sessionmaker)).values())

        wiped = await v.json("POST", "/admin/wipe", headers={"X-Admin-Secret": ADMIN_SECRET})
        worlds = await _worlds(db_sessionmaker)
        assert {UUID(world_id) for world_id in wiped["retired_world_ids"]} == set(worlds)
        assert {world.status for world in worlds.values()} == {"retired"}
        for world_id in worlds:
            assert await _org_count(db_sessionmaker, world_id) == 0

        # The session survives with no world; start gives it a fresh one, same lineage.
        await v.request("GET", "/session", expect=401)
        restarted = await v.start()
        assert restarted["session_id"] == first["session_id"]
        assert restarted["world_id"] not in {first["world_id"], *map(str, worlds)}
    lineage = (await _visitor_row(db_sessionmaker, first["session_id"])).budget_lineage
    assert worlds[UUID(first["world_id"])].budget_lineage == lineage


# --- SyncRetirer ----------------------------------------------------------------------


async def test_sync_retirer_is_idempotent_and_finishes_interrupted_retirements(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await load_world(db_sessionmaker, "minimal", build_sha="test")
    keeping = SyncRetirer(db_sessionmaker, row_ttl_s=60)
    await keeping.retire(world_id)
    await keeping.retire(world_id)
    assert [e.type for e in await _events(db_sessionmaker, world_id)] == [
        "world.created",
        "world.retired",
    ]
    assert await _org_count(db_sessionmaker, world_id) == 1

    await SyncRetirer(db_sessionmaker).retire(world_id)
    assert await _org_count(db_sessionmaker, world_id) == 0
    assert (await _worlds(db_sessionmaker))[world_id].status == "retired"
    await SyncRetirer(db_sessionmaker).retire(uuid4())
