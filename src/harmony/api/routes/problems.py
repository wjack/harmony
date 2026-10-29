"""Typed error bodies for the P1–P2 routes.

Every P1–P2 route answers its own errors with an `ApiProblem` (`{detail, code, ...}`)
by raising `ApiProblemError`; `create_app` registers `problem_handler` for it. Errors
raised before a route runs (401 from `current_actor`, 422 validation) keep FastAPI's
bodies, except that a 422 never echoes the input back (`validation_handler`).

How the API maps workflow and domain errors:
- `WorkflowUpdateFailedError` whose cause is an `ApplicationError` of type
  `CARD_NOT_PENDING_ERROR` / `ESCALATION_NOT_OPEN_ERROR` / `NOT_IN_ORG_ERROR`
  (`temporal.workflows.payloads`), or the matching `domain.errors` exception → 409
  `card_not_pending` / 409 `escalation_not_open` / 403 `not_in_org`;
- `harmony.errors.WorldRetired` → 409 `world_retired`; a non-admin settings write →
  403 `not_admin`; something the acting member can't see → 404 `not_found` (never 403).
- The `steer` Update's `STEER_RUNNING_ERROR` → 409 `steer_running` with `by` (who runs
  it), `THREAD_CLOSED_ERROR` → 409 `thread_closed`, `STEER_CONFLICT_ERROR` → 422
  `invalid`.
- A Retry's `NOTHING_TO_RETRY_ERROR` → 409 `nothing_to_retry`: no refused step is waiting
  for one, or something has happened since.

Writes that name the member their tab shows (`as_member_id`: steers, reopen, notes)
answer 409 `member_changed` with `member`, the session's current member, before they
read or write anything. Reopen's own conflicts (`thread_open`, `reopen_in_progress`,
`closing_in_progress`, `not_reopenable`) are raised by its route, not mapped here.

The live phase's gate (`api/gate.py`) answers with the same body: 401
`passcode_required` before unlock and `passcode_invalid` for a wrong passcode, 429
`too_many_attempts` (with `Retry-After`) for throttled unlocks and world creation, and
413 `payload_too_large` for an oversized body.

Owner: E4.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal, cast
from uuid import UUID

from fastapi import HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError

from harmony.agents.models import MemberRef
from harmony.api.deps import AppServices, documented
from harmony.auth.views import OrgRef
from harmony.db import repo
from harmony.db.models import Member
from harmony.db.models.choices import ReopenBlock
from harmony.domain.errors import ConsultNotOpen, InvalidRoute
from harmony.temporal.activities.network import workflow_missing
from harmony.temporal.workflows.payloads import (
    CARD_NOT_PENDING_ERROR,
    ESCALATION_NOT_OPEN_ERROR,
    INVALID_RESOLUTION_ERROR,
    NOT_FOUND_ERROR,
    NOT_IN_ORG_ERROR,
    NOTHING_TO_RETRY_ERROR,
    STEER_CONFLICT_ERROR,
    STEER_RUNNING_ERROR,
    THREAD_CLOSED_ERROR,
)

ProblemCode = Literal[
    "not_found",
    "invalid",
    "not_admin",
    "card_not_pending",
    "escalation_not_open",
    "not_in_org",
    "world_retired",
    "passcode_required",
    "passcode_invalid",
    "too_many_attempts",
    "payload_too_large",
    "member_changed",
    "steer_running",
    "thread_closed",
    "thread_open",
    "reopen_in_progress",
    "closing_in_progress",
    "not_reopenable",
    "nothing_to_retry",
]

RETRY_AFTER: Final = "Retry-After"


class ReopenedBy(BaseModel):
    """Who reopened a thread, or is reopening it: the org, and the member only for the
    viewer's own org."""

    model_config = ConfigDict(frozen=True)

    org: OrgRef
    member: MemberRef | None


class ApiProblem(BaseModel):
    """A P1–P2 route's error body."""

    model_config = ConfigDict(frozen=True)

    detail: str
    code: ProblemCode
    # `card_not_pending` / `escalation_not_open`: who settled it first, if a member did.
    resolved_by: MemberRef | None = None
    # `steer_running`: who the running steer is from.
    by: MemberRef | None = None
    # `member_changed`: the session's member now, after a switch in another tab.
    member: MemberRef | None = None
    # `thread_open` / `reopen_in_progress`: who reopened it, or is reopening it.
    reopened_by: ReopenedBy | None = None
    # `not_reopenable`: why.
    block: ReopenBlock | None = None


class ApiProblemError(Exception):
    def __init__(self, status_code: int, problem: ApiProblem) -> None:
        super().__init__(problem.detail)
        self.status_code = status_code
        self.problem = problem


def not_found(what: str) -> ApiProblemError:
    return ApiProblemError(
        status.HTTP_404_NOT_FOUND, ApiProblem(detail=f"{what} not found", code="not_found")
    )


def problem(status_code: int, code: ProblemCode, detail: str, **extra: Any) -> ApiProblemError:
    return ApiProblemError(status_code, ApiProblem(detail=detail, code=code, **extra))


def temporal_of(services: AppServices) -> Client:
    """The app's Temporal client; 503 when the app runs without one (tests)."""
    if services.temporal is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "temporal is not configured")
    return services.temporal


def world_retired() -> ApiProblemError:
    return problem(status.HTTP_409_CONFLICT, "world_retired", "this world was retired")


def invalid(detail: str) -> ApiProblemError:
    return problem(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid", detail)


# Workflow and domain error types (`ApplicationError.type`) → status and problem code.
_BY_TYPE: Final[Mapping[str, tuple[int, ProblemCode]]] = {
    CARD_NOT_PENDING_ERROR: (status.HTTP_409_CONFLICT, "card_not_pending"),
    ESCALATION_NOT_OPEN_ERROR: (status.HTTP_409_CONFLICT, "escalation_not_open"),
    NOT_IN_ORG_ERROR: (status.HTTP_403_FORBIDDEN, "not_in_org"),
    INVALID_RESOLUTION_ERROR: (status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid"),
    "ValueError": (status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid"),
    InvalidRoute.__name__: (status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid"),
    # A consult is answered through its escalation: the same conflict to the client.
    ConsultNotOpen.__name__: (status.HTTP_409_CONFLICT, "escalation_not_open"),
    NOT_FOUND_ERROR: (status.HTTP_404_NOT_FOUND, "not_found"),
    "WorldRetired": (status.HTTP_409_CONFLICT, "world_retired"),
    "NotAdmin": (status.HTTP_403_FORBIDDEN, "not_admin"),
    STEER_RUNNING_ERROR: (status.HTTP_409_CONFLICT, "steer_running"),
    THREAD_CLOSED_ERROR: (status.HTTP_409_CONFLICT, "thread_closed"),
    STEER_CONFLICT_ERROR: (status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid"),
    NOTHING_TO_RETRY_ERROR: (status.HTTP_409_CONFLICT, "nothing_to_retry"),
}
# The `ApplicationError` detail naming the member a problem's field refers to.
_MEMBER_DETAILS: Final[Mapping[ProblemCode, tuple[str, Literal["resolved_by", "by"]]]] = {
    "escalation_not_open": ("resolved_by_member_id", "resolved_by"),
    "steer_running": ("by_member_id", "by"),
}


async def from_update_failure(
    failed: WorkflowUpdateFailedError | ApplicationError,
    sessionmaker: async_sessionmaker[AsyncSession],
    world_id: UUID,
) -> ApiProblemError:
    """The problem a failed Update maps to (module docstring). Unknown failures raise."""
    cause = failed.cause if isinstance(failed, WorkflowUpdateFailedError) else failed
    if not isinstance(cause, ApplicationError) or cause.type not in _BY_TYPE:
        raise failed
    status_code, code = _BY_TYPE[cause.type]
    extra: dict[str, MemberRef | None] = {}
    if code in _MEMBER_DETAILS:
        key, field = _MEMBER_DETAILS[code]
        member_id = _details(cause.details).get(key)
        if isinstance(member_id, str):
            async with sessionmaker() as session:
                extra[field] = await member_ref(session, world_id, UUID(member_id))
    return problem(status_code, code, cause.message, **extra)


async def member_ref(session: AsyncSession, world_id: UUID, member_id: UUID) -> MemberRef | None:
    member = await repo.get(session, Member, world_id, member_id)
    if member is None:
        return None
    return MemberRef(id=member.id, slug=member.slug, name=member.name)


def _details(values: Sequence[Any]) -> dict[str, Any]:
    first = values[0] if values else None
    if not isinstance(first, Mapping):
        return {}
    return {str(key): value for key, value in cast(Mapping[Any, Any], first).items()}


async def problem_handler(request: Request, exc: Exception) -> JSONResponse:
    """`create_app`'s handler for `ApiProblemError`."""
    if not isinstance(exc, ApiProblemError):
        raise exc
    return JSONResponse(exc.problem.model_dump(mode="json"), status_code=exc.status_code)


async def validation_handler(request: Request, exc: Exception) -> JSONResponse:
    """`create_app`'s handler for `RequestValidationError`: FastAPI's 422 body without
    each error's `input`, so a rejected message isn't sent back whole."""
    if not isinstance(exc, RequestValidationError):
        raise exc
    errors = [{key: value for key, value in e.items() if key != "input"} for e in exc.errors()]
    return JSONResponse(
        {"detail": jsonable_encoder(errors)}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
    )


def problems(*codes: int, cursor: bool = False) -> dict[int | str, dict[str, Any]]:
    """`responses=` for a P1–P2 route: `ApiProblem` for each code, 401 with FastAPI's
    body, and the cursor header on 200 when `cursor`."""
    return documented(status.HTTP_401_UNAUTHORIZED, cursor=cursor) | problem_entries(*codes)


def problem_entries(*codes: int) -> dict[int | str, dict[str, Any]]:
    """`responses=` entries answering each code with an `ApiProblem`; a 429 also
    documents `Retry-After`. Merge them into a route's other entries."""
    entries: dict[int | str, dict[str, Any]] = {}
    for code in codes:
        entries[code] = {"model": ApiProblem}
        if code == status.HTTP_429_TOO_MANY_REQUESTS:
            entries[code]["headers"] = {
                RETRY_AFTER: {
                    "description": "Seconds until another attempt may succeed.",
                    "schema": {"type": "integer"},
                }
            }
    return entries


__all__ = [
    "RETRY_AFTER",
    "ApiProblem",
    "ApiProblemError",
    "ProblemCode",
    "ReopenedBy",
    "from_update_failure",
    "invalid",
    "member_ref",
    "not_found",
    "problem",
    "problem_entries",
    "problem_handler",
    "problems",
    "temporal_of",
    "validation_handler",
    "workflow_missing",
    "world_retired",
]
