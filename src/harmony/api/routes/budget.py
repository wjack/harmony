"""Budget route: the visitor's live-model budget (LIVE_DEMO_PLAN 3.5).

- GET /api/budget: whether live models are on, and what this session may still spend

`state` says which cap, if any, stops live turns now: `ok`; `session_exhausted` (the
session's lineage); `daily_exhausted` (today's daily row, or the session's client row);
`demo_exhausted` (the global row, or an active provider block); `paused` (the live
switch). A key counts as exhausted below `EXHAUSTED_BELOW_USD`, and a missing lineage
or client row as a full cap. With hosted caps off it is `{live: false, state: "ok"}`.
When several caps are spent, the widest is reported (`budget_pg.REFUSAL_ORDER`).
The SPA refetches it on a `demo.notice` of kind `budget` (sent when a call is refused
and when the operator pauses or resumes live models) and every 30 s while visible.

It reads the rows only; the worker's reservation stays the one enforcement point, so
a call can still be refused while this reads `ok` (calls in flight hold reservations).

Owner: L2.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Final, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from harmony.api.deps import ServicesDep
from harmony.api.routes.problems import problems
from harmony.auth.session import ActorDep
from harmony.db.models.choices import BudgetKind
from harmony.llm.budget_pg import HostedCaps, next_utc_midnight, utc_today, visitor_budget

router = APIRouter(tags=["budget"])

BudgetState = Literal["ok", "session_exhausted", "daily_exhausted", "demo_exhausted", "paused"]

_STATES: Final[Mapping[BudgetKind, BudgetState]] = MappingProxyType(
    {
        "session": "session_exhausted",
        "client": "daily_exhausted",
        "daily": "daily_exhausted",
        "global": "demo_exhausted",
        "provider": "demo_exhausted",
        "paused": "paused",
    }
)


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class SessionBudget(_View):
    """The session's lineage cap ("Live · $7.40 left")."""

    cap_usd: float
    spent_usd: float
    reserved_usd: float
    remaining_usd: float


class BudgetView(_View):
    live: bool
    state: BudgetState
    # None with hosted caps off.
    session: SessionBudget | None = None
    # When a daily cap resets (00:00 UTC), while `state` is `daily_exhausted`.
    resets_at: datetime | None = None


@router.get("/budget", response_model=BudgetView, responses=problems())
async def get_budget(actor: ActorDep, services: ServicesDep) -> BudgetView:
    caps = HostedCaps.from_settings(services.settings)
    if caps is None:
        return BudgetView(live=False, state="ok")
    today = utc_today()
    budget = await visitor_budget(
        services.sessionmaker, caps, session_id=actor.session_id, today=today
    )
    state = _STATES[budget.stopped_by] if budget.stopped_by is not None else "ok"
    lineage = budget.lineage
    return BudgetView(
        live=True,
        state=state,
        session=SessionBudget(
            cap_usd=float(lineage.cap_usd),
            spent_usd=float(lineage.spent_usd),
            reserved_usd=float(lineage.reserved_usd),
            remaining_usd=float(lineage.remaining_usd),
        ),
        resets_at=next_utc_midnight(today) if state == "daily_exhausted" else None,
    )


__all__ = ["BudgetState", "BudgetView", "SessionBudget", "router"]
