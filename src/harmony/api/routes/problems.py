"""Typed error bodies for the P1–P2 routes.

Every P1–P2 route answers its own errors with an `ApiProblem` (`{detail, code, ...}`)
by raising `ApiProblemError`; `create_app` registers `problem_handler` for it. Errors
raised before a route runs (401 from `current_actor`, 422 validation) keep FastAPI's
bodies.

How the API maps workflow and domain errors:
- `WorkflowUpdateFailedError` whose cause is an `ApplicationError` of type
  `CARD_NOT_PENDING_ERROR` / `ESCALATION_NOT_OPEN_ERROR` / `NOT_IN_ORG_ERROR`
  (`temporal.workflows.payloads`), or the matching `domain.errors` exception → 409
  `card_not_pending` / 409 `escalation_not_open` / 403 `not_in_org`;
- `harmony.errors.WorldRetired` → 409 `world_retired`; a non-admin settings write →
  403 `not_admin`; something the acting member can't see → 404 `not_found` (never 403).

Owner: E4.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal, cast
from uuid import UUID

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError

from harmony.agents.models import MemberRef
from harmony.api.deps import AppServices, documented
from harmony.db import repo
from harmony.db.models import Member
from harmony.domain.errors import ConsultNotOpen, InvalidRoute
from harmony.temporal.activities.network import workflow_missing
from harmony.temporal.workflows.payloads import (
    CARD_NOT_PENDING_ERROR,
    ESCALATION_NOT_OPEN_ERROR,
    INVALID_RESOLUTION_ERROR,
    NOT_FOUND_ERROR,
    NOT_IN_ORG_ERROR,
)

ProblemCode = Literal[
    "not_found",
    "invalid",
    "not_admin",
    "card_not_pending",
    "escalation_not_open",
    "not_in_org",
    "world_retired",
]


class ApiProblem(BaseModel):
    """A P1–P2 route's error body."""

    model_config = ConfigDict(frozen=True)

    detail: str
    code: ProblemCode
    # `card_not_pending` / `escalation_not_open`: who settled it first, if a member did.
    resolved_by: MemberRef | None = None


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
    details = _details(cause.details)
    resolved_by: MemberRef | None = None
    member_id = details.get("resolved_by_member_id")
    if code == "escalation_not_open" and isinstance(member_id, str):
        async with sessionmaker() as session:
            resolved_by = await member_ref(session, world_id, UUID(member_id))
    return problem(status_code, code, cause.message, resolved_by=resolved_by)


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


def problems(*codes: int, cursor: bool = False) -> dict[int | str, dict[str, Any]]:
    """`responses=` for a P1–P2 route: `ApiProblem` for each code, 401 with FastAPI's
    body, and the cursor header on 200 when `cursor`."""
    responses = documented(status.HTTP_401_UNAUTHORIZED, cursor=cursor)
    for code in codes:
        responses[code] = {"model": ApiProblem}
    return responses


__all__ = [
    "ApiProblem",
    "ApiProblemError",
    "ProblemCode",
    "from_update_failure",
    "invalid",
    "member_ref",
    "not_found",
    "problem",
    "problem_handler",
    "problems",
    "temporal_of",
    "workflow_missing",
    "world_retired",
]
