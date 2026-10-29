"""Escalation routes: resolving inbox items (§7, §10, UI_REFERENCE §6).

- POST /api/escalations/{escalation_id}/resolve {action, text?, draft_rev?, also_for?}: the
  escalation panel's actions and the chat's inline answers
- POST /api/escalations/{escalation_id}/reassign {member_id}
- POST /api/escalations/{escalation_id}/retry {draft_text?}: run the step Claude declined
  again (`retry_answer`): the answerer, or the sharing check on the draft as the member
  edited it

Each runs a `ThreadWorkflow` Update (`resolve_escalation`, `reassign`) naming
the acting member and their org (validators can't read the database). Only members of
the escalation's org who can see its thread may act (404 otherwise). A second resolver
gets 409 `escalation_not_open` with `resolved_by`.

One answer, many askers (the chat's card): on a question its own side's representative
asked about a request, an approval may also go to the card's other open items routed to
the member (`also_for`), and the chat's owner's answer in their own words is recorded on
the card and told to the chat (`domain.answers`, `temporal.answers.pass_on`). What it
refuses there is 422 `invalid`.

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
from harmony.domain import answers, refusals
from harmony.temporal.activities.network import run_update
from harmony.temporal.answers import pass_on
from harmony.temporal.workflows.payloads import (
    AnswerUsed,
    EscalationResolved,
    Reassigned,
    Reassignment,
    RetryAnswer,
    thread_workflow_id,
)

router = APIRouter(tags=["escalations"])


class ResolveEscalation(BaseModel):
    """The member's resolution (see `agents.models.EscalationResolution` for what each
    action needs)."""

    action: EscalationAction
    text: str | None = Field(default=None, max_length=20_000)
    draft_rev: int | None = Field(default=None, ge=1)
    # The card's other open items routed to the member that the answer also goes to
    # (`approve` with `text` only).
    also_for: list[UUID] = Field(default_factory=list[UUID], max_length=50)


class ReassignEscalation(BaseModel):
    member_id: UUID


class RetryEscalation(BaseModel):
    # The member's edit of the draft, which the sharing check runs on; omitted runs it
    # on the draft as it stands. Ignored while the item has no draft.
    draft_text: str | None = Field(default=None, min_length=1, max_length=20_000)
    # The member's rewording of the question, which the answerer drafts on, when Claude
    # declined to draft an answer; it replaces the stored question. Only then.
    question: str | None = Field(default=None, min_length=1, max_length=2000)


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
        try:
            answer = await answers.card_answer(
                db,
                escalation,
                member_id=actor.member_id,
                org_id=actor.org_id,
                action=body.action,
                text=body.text,
                also_for=body.also_for,
            )
        except answers.AnswerRefused as refused:
            raise invalid(str(refused)) from None
        also = [await _open_escalation(db, actor, item_id) for item_id in body.also_for]
    resolution = EscalationResolution(
        escalation_id=escalation.id,
        action=body.action,
        text=body.text,
        draft_rev=body.draft_rev,
        member_id=actor.member_id,
        org_id=actor.org_id,
    )
    resolved = await _update(
        services, actor, escalation, "resolve_escalation", resolution, EscalationResolved
    )
    await pass_on(
        temporal_of(services),
        services.sessionmaker,
        world_id=actor.world_id,
        answered=escalation.id,
        answer=answer,
        also=also,
    )
    return resolved


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
    "/escalations/{escalation_id}/retry",
    response_model=AnswerUsed,
    responses=problems(*_ERRORS, status.HTTP_422_UNPROCESSABLE_CONTENT),
)
async def retry_escalation(
    escalation_id: UUID, body: RetryEscalation, actor: ActorDep, services: ServicesDep
) -> AnswerUsed:
    """Run the step Claude declined on a question again, as its next draft (the thread's
    `retry_answer`). Returns once the draft is reserved; the item shows it when written.
    409 `nothing_to_retry` unless Claude declined the step the item waits on."""
    async with services.sessionmaker() as db:
        escalation = await _open_escalation(db, actor, escalation_id)
        consult_id = escalation.consult_id
        if escalation.kind != "consult" or consult_id is None:
            raise invalid("only a question's answer can be retried")
        declined = await refusals.consult_refusals(db, [(consult_id, escalation.current_draft_rev)])
    if consult_id not in declined:
        raise problem(
            status.HTTP_409_CONFLICT, "nothing_to_retry", "Claude didn't decline this question"
        )
    if body.question is not None and declined[consult_id].step != "answer":
        raise invalid("the question can be reworded only when Claude declined to answer it")
    retry = RetryAnswer(
        escalation_id=escalation.id,
        org_id=actor.org_id,
        draft_text=body.draft_text,
        question=body.question,
    )
    return await _update(services, actor, escalation, "retry_answer", retry, AnswerUsed)


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
