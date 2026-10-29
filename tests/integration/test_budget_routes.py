"""`GET /api/budget` and the operator routes (LIVE_DEMO_PLAN 3.4 to 3.6), on the real
schema with live models on. Nothing calls a model."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from harmony.api.app import create_app
from harmony.config import Settings
from harmony.db.models import LiveControls
from harmony.llm.errors import BudgetExhausted
from harmony.llm.live_switch import LiveSwitch, ensure_live_switch
from harmony.temporal.workflows.retire_world import retire_workflow_id
from tests.integration._visitors import ADMIN_SECRET, Visitor, build_app, visitor

type Sessionmaker = async_sessionmaker[AsyncSession]
type Json = dict[str, Any]

ADMIN = {"X-Admin-Secret": ADMIN_SECRET}


@pytest.fixture
def hosted_settings(app_settings: Settings) -> Settings:
    return app_settings.model_copy(
        update={"model_mode": "live", "admin_secret": SecretStr(ADMIN_SECRET)}
    )


@pytest.fixture
async def app(
    hosted_settings: Settings, db_engine: AsyncEngine, db_sessionmaker: Sessionmaker
) -> FastAPI:
    # What the API's lifespan does at boot; these tests skip the lifespan.
    await ensure_live_switch(db_sessionmaker)
    return build_app(hosted_settings, db_engine)


async def _budget(v: Visitor) -> Json:
    return cast(Json, await v.json("GET", "/budget"))


# --- GET /api/budget ---------------------------------------------------------------------------


async def test_with_scripted_models_the_budget_is_not_live(
    app_settings: Settings, db_engine: AsyncEngine
) -> None:
    async with visitor(build_app(app_settings, db_engine)) as v:
        await v.start()
        assert await _budget(v) == {"live": False, "state": "ok"}


async def test_the_state_comes_from_the_live_switch(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        await v.start()
        assert await _budget(v) == {"live": True, "state": "ok"}
        await v.json("POST", "/admin/live", json={"enabled": False}, headers=ADMIN)
        assert await _budget(v) == {"live": True, "state": "paused"}
        await v.json("POST", "/admin/live", json={"enabled": True}, headers=ADMIN)
        await LiveSwitch(db_sessionmaker).block_provider()
        assert await _budget(v) == {"live": True, "state": "demo_exhausted"}


async def test_the_budget_needs_a_session(app: FastAPI) -> None:
    async with visitor(app) as v:
        await v.json("GET", "/budget", expect=401)


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
        ("GET", "/admin/live", None),
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
    async with visitor(app) as v:
        await v.start()
        paused = await v.json("POST", "/admin/live", json={"enabled": False}, headers=ADMIN)
        assert paused == {"paused": True, "provider_blocked_until": None}
        assert await v.json("POST", "/admin/live", json={"enabled": False}, headers=ADMIN) == paused
        assert (await _budget(v))["state"] == "paused"
        with pytest.raises(BudgetExhausted) as refused:
            await LiveSwitch(db_sessionmaker).check(Decimal(1))
        assert refused.value.kind == "paused"

        await LiveSwitch(db_sessionmaker).block_provider()
        resumed = await v.json("POST", "/admin/live", json={"enabled": True}, headers=ADMIN)
        assert resumed == {"paused": False, "provider_blocked_until": None}
        assert await v.json("POST", "/admin/live", json={"enabled": True}, headers=ADMIN) == resumed
        assert (await _budget(v))["state"] == "ok"
        assert await v.json("GET", "/admin/live", headers=ADMIN) == resumed


async def test_the_world_route_reports_the_retire_workflow(
    hosted_settings: Settings, db_engine: AsyncEngine, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(build_app(hosted_settings, db_engine)) as starter:
        world_id = UUID((await starter.start())["world_id"])
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
