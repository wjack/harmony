"""Escalation routes: resolving inbox items (§7, §10, UI_REFERENCE §6).

- POST /api/escalations/{escalation_id}/resolve {action, text?, draft_rev?}: the
  escalation panel's actions and the chat's inline answers
- POST /api/escalations/{escalation_id}/reassign {member_id}
- POST /api/escalations/{escalation_id}/redraft {instruction}: "Ask Claude to redraft"

Each runs a `ThreadWorkflow` Update (`resolve_escalation`, `reassign`, `redraft`) naming
the acting member and their org (validators can't read the database). Only members of
the escalation's org who can see its thread may act (404 otherwise). A second resolver
gets 409 `escalation_not_open` with `resolved_by`.

An escalation is open only while its thread's workflow waits on it (a thread that ends
cancels its open escalations), so an Update that finds no running workflow is 409
`escalation_not_open` too.

Owner: E4.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import WorkflowUpdateFailedError
from temporalio.service import RPCError

from harmony.agents.models import EscalationAction, EscalationResolution
from harmony.api.deps import AppServices, ServicesDep
from harmony.api.routes.problems import (
    ApiProblemError,
    from_update_failure,
    invalid,
    member_ref,
    not_found,
    problem,
    problems,
    temporal_of,
    workflow_missing,
)
from harmony.auth import access
from harmony.auth.session import Actor, ActorDep
from harmony.db import repo
from harmony.db.models import Escalation, Member, Thread
from harmony.temporal.activities.network import run_update
from harmony.temporal.workflows.payloads import (
    EscalationResolved,
    Reassigned,
    Reassignment,
    Redraft,
    RedraftStarted,
    thread_workflow_id,
)

router = APIRouter(tags=["escalations"])


class ResolveEscalation(BaseModel):
    """The member's resolution (see `agents.models.EscalationResolution` for what each
    action needs)."""

    action: EscalationAction
    text: str | None = Field(default=None, max_length=20_000)
    draft_rev: int | None = Field(default=None, ge=1)


class ReassignEscalation(BaseModel):
    member_id: UUID


class RedraftEscalation(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)


_ERRORS = (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT)


@router.post(
    "/escalations/{escalation_id}/resolve",
    response_model=EscalationResolved,
    responses=problems(*_ERRORS),
)
async def resolve_escalation(
    escalation_id: UUID, body: ResolveEscalation, actor: ActorDep, services: ServicesDep
) -> EscalationResolved:
    async with services.sessionmaker() as db:
        escalation = await _open_escalation(db, actor, escalation_id)
    if body.action == "revise" and (
        escalation.kind != "outbound_review" or not (body.text or "").strip()
    ):
        raise invalid("revise sends a held message back with what to change, so it needs text")
    resolution = EscalationResolution(
        escalation_id=escalation.id,
        action=body.action,
        text=body.text,
        draft_rev=body.draft_rev,
        member_id=actor.member_id,
        org_id=actor.org_id,
    )
    return await _update(
        services, actor, escalation, "resolve_escalation", resolution, EscalationResolved
    )


@router.post(
    "/escalations/{escalation_id}/reassign",
    response_model=Reassigned,
    responses=problems(*_ERRORS),
)
async def reassign_escalation(
    escalation_id: UUID, body: ReassignEscalation, actor: ActorDep, services: ServicesDep
) -> Reassigned:
    async with services.sessionmaker() as db:
        escalation = await _open_escalation(db, actor, escalation_id)
        target = await repo.get(db, Member, actor.world_id, body.member_id)
        if target is None or target.org_id != escalation.org_id:
            raise not_found("member")
    reassignment = Reassignment(
        escalation_id=escalation.id, to_member_id=target.id, by_member_id=actor.member_id
    )
    return await _update(services, actor, escalation, "reassign", reassignment, Reassigned)


@router.post(
    "/escalations/{escalation_id}/redraft",
    response_model=RedraftStarted,
    responses=problems(*_ERRORS),
)
async def redraft_escalation(
    escalation_id: UUID, body: RedraftEscalation, actor: ActorDep, services: ServicesDep
) -> RedraftStarted:
    async with services.sessionmaker() as db:
        escalation = await _open_escalation(db, actor, escalation_id)
    redraft = Redraft(
        escalation_id=escalation.id, instruction=body.instruction, by_member_id=actor.member_id
    )
    return await _update(services, actor, escalation, "redraft", redraft, RedraftStarted)


async def _open_escalation(db: AsyncSession, actor: Actor, escalation_id: UUID) -> Escalation:
    """The escalation, if the actor may act on it: 404 when they can't see it, 409
    `escalation_not_open` (with who resolved it) when it's settled."""
    escalation = await repo.get(db, Escalation, actor.world_id, escalation_id)
    if escalation is None or escalation.thread_id is None or escalation.org_id != actor.org_id:
        raise not_found("escalation")
    visible = await db.scalar(
        access.visible_threads(actor)
        .where(Thread.id == escalation.thread_id)
        .with_only_columns(Thread.id)
    )
    if visible is None:
        raise not_found("escalation")
    if escalation.status != "open":
        raise await _not_open(db, escalation)
    return escalation


async def _not_open(db: AsyncSession, escalation: Escalation) -> ApiProblemError:
    resolved_by = (
        await member_ref(db, escalation.world_id, escalation.resolved_by_member_id)
        if escalation.resolved_by_member_id is not None
        else None
    )
    return problem(
        status.HTTP_409_CONFLICT,
        "escalation_not_open",
        f"escalation {escalation.id} is {escalation.status}",
        resolved_by=resolved_by,
    )


async def _update[R](
    services: AppServices,
    actor: Actor,
    escalation: Escalation,
    update: str,
    arg: Any,
    result_type: type[R],
) -> R:
    client = temporal_of(services)
    assert escalation.thread_id is not None
    handle = client.get_workflow_handle(thread_workflow_id(actor.world_id, escalation.thread_id))
    try:
        return await run_update(handle, update, arg, result_type)
    except WorkflowUpdateFailedError as failed:
        raise await from_update_failure(failed, services.sessionmaker, actor.world_id) from None
    except RPCError as error:
        if not workflow_missing(error):
            raise
    async with services.sessionmaker() as db:
        current = await repo.get_required(db, Escalation, actor.world_id, escalation.id)
        raise await _not_open(db, current)
