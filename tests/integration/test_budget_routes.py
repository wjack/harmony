"""`GET /api/budget` and the operator routes (LIVE_DEMO_PLAN 3.4 to 3.6), on the real
schema with the hosted caps on (`HOSTED_CAPS_IN_DEV`). Nothing calls a model."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from harmony.api.app import create_app
from harmony.config import Settings
from harmony.db.models import Budget, LiveControls, VisitorSession
from harmony.llm.budget_pg import (
    EXHAUSTED_BELOW_USD,
    GLOBAL_KEY,
    HostedBudgets,
    client_budget_key,
    daily_key,
    ensure_budget,
    ensure_hosted_rows,
    lineage_key,
    set_live,
    utc_today,
)
from harmony.llm.errors import BudgetExhausted
from harmony.temporal.workflows.retire_world import retire_workflow_id
from tests.integration._visitors import ADMIN_SECRET, Visitor, build_app, visitor

type Sessionmaker = async_sessionmaker[AsyncSession]
type Json = dict[str, Any]

CLIENT_HASH = "c" * 32
CAPS: dict[str, Any] = {
    "model_mode": "live",
    "hosted_caps_in_dev": True,
    "lineage_budget_usd": Decimal(10),
    "client_daily_budget_usd": Decimal(25),
    "daily_budget_usd": Decimal(60),
    "global_budget_usd": Decimal(250),
}
ADMIN = {"X-Admin-Secret": ADMIN_SECRET}


@pytest.fixture
def hosted_settings(app_settings: Settings) -> Settings:
    return app_settings.model_copy(update={**CAPS, "admin_secret": SecretStr(ADMIN_SECRET)})


@pytest.fixture
async def app(
    hosted_settings: Settings, db_engine: AsyncEngine, db_sessionmaker: Sessionmaker
) -> FastAPI:
    # What the API's lifespan does at boot; these tests skip the lifespan.
    await ensure_hosted_rows(db_sessionmaker, hosted_settings)
    return build_app(hosted_settings, db_engine)


@dataclass(frozen=True)
class Started:
    session_id: UUID
    world_id: UUID
    lineage: UUID


async def _start(v: Visitor, sm: Sessionmaker) -> Started:
    view = await v.start()
    session_id = UUID(view["session_id"])
    async with sm() as session:
        lineage = await session.scalar(
            select(VisitorSession.budget_lineage).where(VisitorSession.id == session_id)
        )
    assert lineage is not None
    return Started(session_id, UUID(view["world_id"]), lineage)


async def _set_spent(sm: Sessionmaker, key: str, cap: Decimal, *, left: Decimal) -> None:
    """Create `key` if needed and spend it down to `left` remaining."""
    async with sm() as session, session.begin():
        await ensure_budget(session, key, cap)
        await session.execute(update(Budget).where(Budget.key == key).values(spent_usd=cap - left))


async def _budget(v: Visitor) -> Json:
    return cast(Json, await v.json("GET", "/budget"))


def _next_midnight() -> str:
    day: date = utc_today() + timedelta(days=1)
    return datetime.combine(day, time.min, tzinfo=UTC).isoformat().replace("+00:00", "Z")


# --- GET /api/budget ---------------------------------------------------------------------------


async def test_with_caps_off_the_budget_is_not_live(
    app_settings: Settings, db_engine: AsyncEngine
) -> None:
    async with visitor(build_app(app_settings, db_engine)) as v:
        await v.start()
        assert await _budget(v) == {
            "live": False,
            "state": "ok",
            "session": None,
            "resets_at": None,
        }


async def test_a_fresh_session_has_its_whole_cap(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        started = await _start(v, db_sessionmaker)
        # No lineage or client row yet: both count as full caps.
        async with db_sessionmaker() as session, session.begin():
            await session.execute(
                update(VisitorSession)
                .where(VisitorSession.id == started.session_id)
                .values(client_key_hash=CLIENT_HASH)
            )
        assert await _budget(v) == {
            "live": True,
            "state": "ok",
            "session": {
                "cap_usd": 10.0,
                "spent_usd": 0.0,
                "reserved_usd": 0.0,
                "remaining_usd": 10.0,
            },
            "resets_at": None,
        }


type Exhaust = Callable[[Sessionmaker, Started], Awaitable[None]]


async def _spend_lineage(sm: Sessionmaker, started: Started) -> None:
    await _set_spent(sm, lineage_key(started.lineage), Decimal(10), left=Decimal("0.5"))


async def _spend_day(sm: Sessionmaker, started: Started) -> None:
    await _set_spent(sm, daily_key(utc_today()), Decimal(60), left=Decimal("0.5"))


async def _spend_client(sm: Sessionmaker, started: Started) -> None:
    async with sm() as session, session.begin():
        await session.execute(
            update(VisitorSession)
            .where(VisitorSession.id == started.session_id)
            .values(client_key_hash=CLIENT_HASH)
        )
    await _set_spent(sm, client_budget_key(CLIENT_HASH, utc_today()), Decimal(25), left=Decimal(0))


async def _spend_demo(sm: Sessionmaker, started: Started) -> None:
    await _set_spent(sm, GLOBAL_KEY, Decimal(250), left=Decimal(1))


async def _block_provider(sm: Sessionmaker, started: Started) -> None:
    await HostedBudgets(sm).block_provider()


async def _pause(sm: Sessionmaker, started: Started) -> None:
    await set_live(sm, enabled=False)


@pytest.mark.parametrize(
    ("exhaust", "state"),
    [
        (_spend_lineage, "session_exhausted"),
        (_spend_day, "daily_exhausted"),
        (_spend_client, "daily_exhausted"),
        (_spend_demo, "demo_exhausted"),
        (_block_provider, "demo_exhausted"),
        (_pause, "paused"),
    ],
    ids=["lineage", "daily", "client", "global", "provider-block", "paused"],
)
async def test_the_state_comes_from_the_rows(
    app: FastAPI, db_sessionmaker: Sessionmaker, exhaust: Exhaust, state: str
) -> None:
    async with visitor(app) as v:
        started = await _start(v, db_sessionmaker)
        assert (await _budget(v))["state"] == "ok"
        await exhaust(db_sessionmaker, started)
        budget = await _budget(v)
        assert budget["state"] == state
        assert budget["resets_at"] == (_next_midnight() if state == "daily_exhausted" else None)


async def test_a_cap_is_exhausted_below_the_threshold(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        started = await _start(v, db_sessionmaker)
        key = lineage_key(started.lineage)
        await _set_spent(db_sessionmaker, key, Decimal(10), left=EXHAUSTED_BELOW_USD)
        budget = await _budget(v)
        assert (budget["state"], budget["session"]["remaining_usd"]) == (
            "ok",
            float(EXHAUSTED_BELOW_USD),
        )
        await _set_spent(
            db_sessionmaker, key, Decimal(10), left=EXHAUSTED_BELOW_USD - Decimal("0.01")
        )
        assert (await _budget(v))["state"] == "session_exhausted"


async def test_each_visitor_sees_their_own_lineage(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as spent, visitor(app) as fresh:
        started = await _start(spent, db_sessionmaker)
        await _start(fresh, db_sessionmaker)
        await _spend_lineage(db_sessionmaker, started)
        assert (await _budget(spent))["state"] == "session_exhausted"
        assert (await _budget(spent))["session"]["spent_usd"] == 9.5
        assert (await _budget(fresh))["state"] == "ok"
        assert (await _budget(fresh))["session"]["spent_usd"] == 0.0


# --- Operator routes -------------------------------------------------------------------------


class _Handle:
    def __init__(self, outcome: WorkflowExecutionStatus | RPCError) -> None:
        self._outcome = outcome

    async def describe(self) -> Any:
        if isinstance(self._outcome, RPCError):
            raise self._outcome
        return _Description(self._outcome)


@dataclass(frozen=True)
class _Description:
    status: WorkflowExecutionStatus


class FakeTemporal:
    """Answers `describe` for the workflows it knows, NOT_FOUND for the rest."""

    def __init__(self) -> None:
        self.workflows: dict[str, WorkflowExecutionStatus | RPCError] = {}

    def get_workflow_handle(self, workflow_id: str) -> _Handle:
        missing = RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b"")
        return _Handle(self.workflows.get(workflow_id, missing))


async def test_the_operator_routes_need_the_admin_secret(
    hosted_settings: Settings, db_engine: AsyncEngine
) -> None:
    routes = [
        ("GET", "/admin/spend", None),
        ("POST", "/admin/live", {"enabled": False}),
        ("GET", f"/admin/worlds/{uuid4()}", None),
        ("GET", "/admin/client-key", None),
    ]
    unset = build_app(hosted_settings, db_engine, admin_secret=None)
    async with visitor(build_app(hosted_settings, db_engine)) as v, visitor(unset) as anyone:
        for method, path, body in routes:
            for headers in ({}, {"X-Admin-Secret": "wrong"}):
                refused = await v.json(method, path, json=body, headers=headers, expect=403)
                assert refused["code"] == "not_admin"
            refused = await anyone.json(method, path, json=body, headers=ADMIN, expect=403)
            assert refused["code"] == "not_admin"


async def test_pause_on_a_database_without_the_switch_row(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with db_sessionmaker() as session, session.begin():
        await session.execute(delete(LiveControls))
    global_ = {"cap_usd": 250.0, "spent_usd": 0.0, "reserved_usd": 0.0}
    async with visitor(app) as v:
        await _start(v, db_sessionmaker)
        paused = await v.json("POST", "/admin/live", json={"enabled": False}, headers=ADMIN)
        assert paused == {"paused": True, "provider_blocked_until": None, "global": global_}
        assert await v.json("POST", "/admin/live", json={"enabled": False}, headers=ADMIN) == paused
        assert (await _budget(v))["state"] == "paused"
        with pytest.raises(BudgetExhausted) as refused:
            await HostedBudgets(db_sessionmaker).reserve(uuid4(), [GLOBAL_KEY], Decimal(1))
        assert refused.value.kind == "paused"

        await HostedBudgets(db_sessionmaker).block_provider()
        resumed = await v.json("POST", "/admin/live", json={"enabled": True}, headers=ADMIN)
        assert resumed == {"paused": False, "provider_blocked_until": None, "global": global_}
        assert await v.json("POST", "/admin/live", json={"enabled": True}, headers=ADMIN) == resumed
        assert (await _budget(v))["state"] == "ok"
        spend = await v.json("GET", "/admin/spend", headers=ADMIN)
        assert spend["live"] == resumed


async def test_the_spend_report_names_the_top_spenders(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    sm = db_sessionmaker
    heavy, light = lineage_key(uuid4()), lineage_key(uuid4())
    client = client_budget_key(CLIENT_HASH, utc_today())
    today = daily_key(utc_today())
    hosted = HostedBudgets(sm)
    for lineage, cost in ((heavy, Decimal(3)), (light, Decimal(1))):
        async with sm() as session, session.begin():
            await ensure_budget(session, lineage, Decimal(10))
            await ensure_budget(session, client, Decimal(25))
        reservation = uuid4()
        await hosted.reserve(reservation, [lineage, client, today, GLOBAL_KEY], Decimal(4))
        await hosted.settle(reservation, cost)
    # A day-old client row is not today's.
    await _set_spent(
        sm, client_budget_key("d" * 32, date(2026, 1, 1)), Decimal(25), left=Decimal(0)
    )

    async with visitor(app) as v:
        spend = await v.json("GET", "/admin/spend", headers=ADMIN)
    assert spend["today"] == {"cap_usd": 60.0, "spent_usd": 4.0, "reserved_usd": 0.0}
    assert spend["live"]["global"] == {"cap_usd": 250.0, "spent_usd": 4.0, "reserved_usd": 0.0}
    assert spend["lineages_today"] == 2
    assert [(row["key"], row["spent_usd"]) for row in spend["top_lineages"]] == [
        (heavy, 3.0),
        (light, 1.0),
    ]
    assert [(row["key"], row["spent_usd"]) for row in spend["top_clients"]] == [(client, 4.0)]


async def test_the_world_route_reports_the_retire_workflow(
    hosted_settings: Settings, db_engine: AsyncEngine, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(build_app(hosted_settings, db_engine)) as starter:
        world_id = (await _start(starter, db_sessionmaker)).world_id
    temporal = FakeTemporal()
    app = create_app(hosted_settings, engine=db_engine, temporal=cast(Client, temporal))
    async with visitor(app) as v:
        path = f"/admin/worlds/{world_id}"

        assert await v.json("GET", path, headers=ADMIN) == {
            "status": "live",
            "retire_workflow": None,
        }
        for status, shown in (
            (WorkflowExecutionStatus.RUNNING, "running"),
            (WorkflowExecutionStatus.COMPLETED, "completed"),
            (WorkflowExecutionStatus.FAILED, "failed"),
            (WorkflowExecutionStatus.TERMINATED, "terminated"),
        ):
            temporal.workflows[retire_workflow_id(world_id)] = status
            view = await v.json("GET", path, headers=ADMIN)
            assert view == {"status": "live", "retire_workflow": shown}

        missing = await v.json("GET", f"/admin/worlds/{uuid4()}", headers=ADMIN, expect=404)
        assert missing["code"] == "not_found"


async def test_the_client_key_route_shows_the_callers_headers(
    app: FastAPI, hosted_settings: Settings
) -> None:
    """The key is the gate's own: the rightmost forwarded hop, the one the proxy adds."""
    headers = {
        **ADMIN,
        "X-Forwarded-For": "198.51.100.9, 203.0.113.7",
        "True-Client-IP": "198.51.100.9",
    }
    async with visitor(app) as v:
        view = await v.json("GET", "/admin/client-key", headers=headers)
        proxied = {**ADMIN, "X-Forwarded-For": "203.0.113.7, 10.0.0.1"}
        behind_a_proxy = await v.json("GET", "/admin/client-key", headers=proxied)
    assert view == {
        "source": hosted_settings.client_ip_header,
        "key": "203.0.113.7",
        "forwarded_for": ["198.51.100.9", "203.0.113.7"],
        "true_client_ip": "198.51.100.9",
        "cf_connecting_ip": None,
    }
    # A private address in the proxy's position is an unknown key.
    assert behind_a_proxy["key"] is None
