"""Budget route: whether live models can run (LIVE_DEMO_PLAN 3.5).

- GET /api/budget: whether this deployment runs live models, and what stops them now

There are no in-app spend caps. `state` is `ok`; `paused` (the live switch); or
`demo_exhausted` (a provider block, set when the Anthropic Console's spend limit refused
a call). With scripted models it is `{live: false, state: "ok"}`. The SPA refetches it on
a `demo.notice` of kind `budget` (sent when a call is refused and when the operator
pauses or resumes live models) and every 30 s while visible.

Owner: L2.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from harmony.api.deps import ServicesDep
from harmony.api.routes.problems import problems
from harmony.auth.session import current_actor
from harmony.db.models.choices import BudgetKind
from harmony.llm.live_switch import live_report

router = APIRouter(tags=["budget"])

BudgetState = Literal["ok", "demo_exhausted", "paused"]

# The live switch reports only `paused` and `provider`; the dev ledger's `global` is
# local and refuses only at the call.
_STATES: Final[Mapping[BudgetKind, BudgetState]] = MappingProxyType(
    {"global": "demo_exhausted", "provider": "demo_exhausted", "paused": "paused"}
)


class BudgetView(BaseModel):
    model_config = ConfigDict(frozen=True)

    live: bool
    state: BudgetState


# A visitor's read: it needs a session, like every other.
@router.get(
    "/budget",
    response_model=BudgetView,
    responses=problems(),
    dependencies=[Depends(current_actor)],
)
async def get_budget(services: ServicesDep) -> BudgetView:
    if services.settings.model_mode != "live":
        return BudgetView(live=False, state="ok")
    stopped_by = (await live_report(services.sessionmaker)).stopped_by
    return BudgetView(live=True, state=_STATES[stopped_by] if stopped_by is not None else "ok")


__all__ = ["BudgetState", "BudgetView", "router"]
