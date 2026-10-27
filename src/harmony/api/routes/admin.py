"""Operator routes (LIVE_DEMO_PLAN 3.4, 3.6). Each takes `X-Admin-Secret` and passes
the passcode gate with it; a missing or wrong secret without the gate's cookie gets the
gate's 401 `passcode_required`, and with the cookie 403 `not_admin`.

- GET /api/admin/spend: the live switch, global, today, the number of lineages today,
  the top 10 lineages and the top 5 client keys by spend
- POST /api/admin/live {enabled}: pause or resume live models (`live_controls`);
  resuming also clears a provider block, and never touches a cap. Every live world gets
  `demo.notice {kind: "budget"}`, so open tabs refetch the budget at once
- GET /api/admin/worlds/{world_id}: a world's status and its retire workflow's
- GET /api/admin/client-key: the caller's own client key and the headers it came from

`POST /api/admin/wipe` stays in `routes/worlds.py`. With ADMIN_SECRET unset every
operator route is refused.

Owner: L2.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from harmony.api import gate
from harmony.api.deps import ServicesDep
from harmony.api.routes.problems import not_found, problem, problem_entries, temporal_of
from harmony.config import ClientIpHeader, Settings
from harmony.db.models import World
from harmony.db.models.choices import WorldStatus
from harmony.db.uow import unit_of_work
from harmony.errors import WorldRetired
from harmony.events.schemas import DemoNotice, EventScope
from harmony.llm import budget_pg
from harmony.temporal.workflows.retire_world import retire_workflow_id

router = APIRouter(tags=["admin"])

# A retire workflow's status (`retire_workflow_id`), from the API's Temporal client.
RetireWorkflowStatus = Literal["running", "completed", "failed", "terminated"]

_ERRORS: Final = (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
# A continued-as-new retirement is still running under its next run.
_RETIRE_STATUSES: Final[Mapping[WorkflowExecutionStatus, RetireWorkflowStatus]] = MappingProxyType(
    {
        WorkflowExecutionStatus.RUNNING: "running",
        WorkflowExecutionStatus.CONTINUED_AS_NEW: "running",
        WorkflowExecutionStatus.COMPLETED: "completed",
        WorkflowExecutionStatus.FAILED: "failed",
        WorkflowExecutionStatus.TIMED_OUT: "failed",
        WorkflowExecutionStatus.TERMINATED: "terminated",
        WorkflowExecutionStatus.CANCELED: "terminated",
    }
)

AdminSecret = Annotated[str | None, Header()]


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class BudgetTotals(_View):
    cap_usd: float
    spent_usd: float
    reserved_usd: float


class KeySpend(BudgetTotals):
    """One budget row: a `lineage:` or `client:` key."""

    key: str


class LiveState(_View):
    """The live switch, as `POST /api/admin/live` leaves it."""

    model_config = ConfigDict(frozen=True, validate_by_name=True, validate_by_alias=True)

    paused: bool
    # Set after the provider's own spend limit refused a call; live calls are refused
    # locally until then.
    provider_blocked_until: datetime | None
    # The `global` budget row; None when no global cap is configured (local runs).
    global_: BudgetTotals | None = Field(alias="global")


class AdminSpendView(_View):
    live: LiveState
    # Today's `daily:` row; None before today's first live call with no daily cap.
    today: BudgetTotals | None
    lineages_today: int
    top_lineages: list[KeySpend]
    top_clients: list[KeySpend]


class LiveToggle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class AdminWorldView(_View):
    status: WorldStatus
    # None when no retire workflow exists for the world.
    retire_workflow: RetireWorkflowStatus | None


class ClientKeyView(_View):
    """What the gate derives for the caller (3.2), for the step-10 header check."""

    source: ClientIpHeader
    # The derived key; None when it is unknown (missing, unparseable or private).
    key: str | None
    forwarded_for: list[str]
    true_client_ip: str | None
    cf_connecting_ip: str | None


@router.get("/admin/spend", response_model=AdminSpendView, responses=problem_entries(*_ERRORS))
async def get_spend(services: ServicesDep, x_admin_secret: AdminSecret = None) -> AdminSpendView:
    require_admin(services.settings, x_admin_secret)
    report = await budget_pg.spend_report(services.sessionmaker)
    return AdminSpendView(
        live=_live_state(report.live),
        today=_totals(report.today) if report.today is not None else None,
        lineages_today=report.lineages_today,
        top_lineages=[_key_spend(row) for row in report.top_lineages],
        top_clients=[_key_spend(row) for row in report.top_clients],
    )


@router.post("/admin/live", response_model=LiveState, responses=problem_entries(*_ERRORS))
async def set_live(
    body: LiveToggle, services: ServicesDep, x_admin_secret: AdminSecret = None
) -> LiveState:
    require_admin(services.settings, x_admin_secret)
    report = await budget_pg.set_live(services.sessionmaker, enabled=body.enabled)
    await _tell_live_worlds(services.sessionmaker)
    return _live_state(report)


@router.get(
    "/admin/worlds/{world_id}",
    response_model=AdminWorldView,
    responses=problem_entries(*_ERRORS, status.HTTP_404_NOT_FOUND),
)
async def get_world(
    world_id: UUID, services: ServicesDep, x_admin_secret: AdminSecret = None
) -> AdminWorldView:
    require_admin(services.settings, x_admin_secret)
    async with services.sessionmaker() as session:
        world_status = await session.scalar(select(World.status).where(World.id == world_id))
    if world_status is None:
        raise not_found("world")
    handle = temporal_of(services).get_workflow_handle(retire_workflow_id(world_id))
    try:
        description = await handle.describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"temporal: {exc.message}"
            ) from exc
        return AdminWorldView(status=world_status, retire_workflow=None)
    retire = _RETIRE_STATUSES.get(description.status) if description.status else None
    return AdminWorldView(status=world_status, retire_workflow=retire)


@router.get("/admin/client-key", response_model=ClientKeyView, responses=problem_entries(*_ERRORS))
async def get_client_key(
    request: Request, services: ServicesDep, x_admin_secret: AdminSecret = None
) -> ClientKeyView:
    settings = services.settings
    require_admin(settings, x_admin_secret)
    headers = request.headers
    forwarded = headers.get("x-forwarded-for")
    return ClientKeyView(
        source=settings.client_ip_header,
        key=gate.client_key(request.scope, settings),
        forwarded_for=[hop.strip() for hop in forwarded.split(",")] if forwarded else [],
        true_client_ip=headers.get("true-client-ip"),
        cf_connecting_ip=headers.get("cf-connecting-ip"),
    )


def require_admin(settings: Settings, secret: str | None) -> None:
    """403 `not_admin` unless `X-Admin-Secret` matches ADMIN_SECRET (never with it
    unset). The gate already sent a caller with neither the cookie nor the secret away
    with 401. `POST /api/admin/wipe` uses it too."""
    expected = settings.admin_secret.get_secret_value() if settings.admin_secret else None
    if (
        expected is None
        or secret is None
        or not hmac.compare_digest(secret.encode(), expected.encode())
    ):
        raise problem(status.HTTP_403_FORBIDDEN, "not_admin", "the admin secret is required")


async def _tell_live_worlds(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """A budget notice to every live world, so a pause or resume reaches open tabs now
    rather than on their next budget poll."""
    async with sessionmaker() as session:
        world_ids = list(await session.scalars(select(World.id).where(World.status == "live")))
    for world_id in world_ids:
        try:
            async with unit_of_work(sessionmaker, world_id) as uow:
                uow.add_event(DemoNotice(kind="budget"), EventScope.demo())
                await uow.commit()
        except WorldRetired:
            continue  # retired since the read: nobody is watching it


def _totals(totals: budget_pg.Totals) -> BudgetTotals:
    return BudgetTotals(
        cap_usd=float(totals.cap_usd),
        spent_usd=float(totals.spent_usd),
        reserved_usd=float(totals.reserved_usd),
    )


def _key_spend(row: budget_pg.KeyTotals) -> KeySpend:
    return KeySpend(
        key=row.key,
        cap_usd=float(row.cap_usd),
        spent_usd=float(row.spent_usd),
        reserved_usd=float(row.reserved_usd),
    )


def _live_state(report: budget_pg.LiveReport) -> LiveState:
    # By alias: `global` is a keyword, so the typed constructor can't name it.
    return LiveState.model_validate(
        {
            "paused": report.paused,
            "provider_blocked_until": report.provider_blocked_until,
            "global": _totals(report.global_) if report.global_ is not None else None,
        }
    )


__all__ = [
    "AdminSpendView",
    "AdminWorldView",
    "BudgetTotals",
    "ClientKeyView",
    "KeySpend",
    "LiveState",
    "LiveToggle",
    "RetireWorkflowStatus",
    "router",
]
