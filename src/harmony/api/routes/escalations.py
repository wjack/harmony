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

One answer, many askers (the chat's card). On a question its own side's representative
asked about a request (requester side), an approval may also go to the card's other
open items routed to the member (`also_for`: each gets a rev drafted from the answer,
through the `use_answer` Update, once this one is resolved). The answer goes only to the
askers: the item answered and those in `also_for`. When the chat's owner answers in their
own words, the card stores their organizations (`consults.add_goes_to`, for "Where your
answer went"), and the chat is told (`network_news`, `answered`), so its Claude can ask
whether part of the answer may go to the request's other conversations. The answer
mustn't name another organization on the request: 422 `invalid` when it does, as for
`also_for` on anything but the chat owner's approval, in their own words, of an item
routed to them, or with an item that isn't theirs on the same card.

An escalation is open only while its thread's workflow waits on it (a thread that ends
cancels its open escalations), so an Update that finds no running workflow is 409
`escalation_not_open` too.

Owner: E4.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import WorkflowUpdateFailedError
from temporalio.service import RPCError

from harmony.agents import context
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
from harmony.db.models import Escalation, Member, NetworkRequest, Org, Thread
from harmony.db.uow import unit_of_work
from harmony.domain import consults, refusals, requests
from harmony.temporal.activities.network import nudge_request_chat, run_update
from harmony.temporal.workflows.payloads import (
    AnswerUsed,
    EscalationResolved,
    NetworkNews,
    Reassigned,
    Reassignment,
    RetryAnswer,
    UseAnswer,
    thread_workflow_id,
)

log = logging.getLogger(__name__)
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


@dataclass(frozen=True, slots=True)
class _CardAnswer:
    """The chat owner's answer, in their own words, to their side's question on a
    request: its card, and the organizations of the askers it goes to."""

    card: UUID
    request_id: UUID
    goes_to: list[UUID]


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
        answer = await _card_answer(db, actor, escalation, body)
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
    # Stored once the answer is given, so a refused one never changes the card.
    if answer is not None:
        async with unit_of_work(services.sessionmaker, actor.world_id) as uow:
            await consults.add_goes_to(uow, answer.card, answer.goes_to)
            await uow.commit()
    for item in also:
        use = UseAnswer(escalation_id=item.id, from_escalation_id=escalation.id)
        try:
            await _update(services, actor, item, "use_answer", use, AnswerUsed)
        except ApiProblemError as refused:
            # It stays open for a person, and the card shows it so.
            log.info("escalation %s didn't take the answer: %s", item.id, refused.problem.detail)
    if answer is not None:
        # Best effort: the answer is given either way.
        try:
            await nudge_request_chat(
                temporal_of(services),
                services.sessionmaker,
                world_id=actor.world_id,
                news=NetworkNews(request_id=answer.request_id, answered=True),
            )
        except Exception:
            log.warning("couldn't tell the chat of request %s", answer.request_id, exc_info=True)
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
    retry = RetryAnswer(
        escalation_id=escalation.id, org_id=actor.org_id, draft_text=body.draft_text
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


async def _card_answer(
    db: AsyncSession, actor: Actor, escalation: Escalation, body: ResolveEscalation
) -> _CardAnswer | None:
    """The chat owner's answer to a requester-side question, once `also_for` and the
    answer's names are checked (module docstring); None for any other resolution."""
    asked = escalation.kind == "consult" and escalation.side == "requester"
    if not asked or escalation.consult_id is None or escalation.thread_id is None:
        if body.also_for:
            raise invalid("only your side's own questions on a request share an answer")
        return None
    text = (body.text or "").strip()
    own_words = body.action == "approve" and bool(text)
    if body.also_for and not own_words:
        raise invalid("only an answer in your own words goes to other conversations")
    thread = await repo.get_required(db, Thread, actor.world_id, escalation.thread_id)
    request = await repo.get_required(db, NetworkRequest, actor.world_id, thread.request_id)
    owner = actor.member_id == request.created_by_member_id
    mine = str(actor.member_id) in escalation.assignees
    if body.also_for and not (owner and mine):
        raise invalid("only the chat's owner shares an answer, from a question routed to them")
    consult = await consults.get(db, world_id=actor.world_id, consult_id=escalation.consult_id)
    card = consults.card_of(consult)
    goes_to = [thread.responder_org_id]
    if body.also_for:
        askers = {
            item.id: item
            for item in await consults.card_askers(db, actor.world_id, card, actor.member_id)
        }
        strays = [item_id for item_id in body.also_for if item_id not in askers]
        if escalation.id in body.also_for or strays:
            raise invalid("also_for takes only your own open items on this card")
        for item_id in body.also_for:
            other_id = askers[item_id].thread_id
            assert other_id is not None
            other = await repo.get_required(db, Thread, actor.world_id, other_id)
            goes_to.append(other.responder_org_id)
    recipients = [row.org_id for row in requests.recipients_of(request)]
    if own_words:
        orgs = {org.id: org.name for org in await repo.list_in_world(db, Org, actor.world_id)}
        # Short names count ("Northlake"), unless the shown asker's or ours starts the same.
        kept = (orgs[thread.responder_org_id], orgs[actor.org_id])
        others = [orgs[org_id] for org_id in recipients if org_id != thread.responder_org_id]
        named = [name for name in others if context.names(text, name, keep=kept)]
        if named:
            # The asker shown gets the words as written (DESIGN §2, D2).
            leave = "Leave the name out." if len(named) == 1 else "Leave the names out."
            raise invalid(
                f"Your answer names {' and '.join(named)}, and "
                f"{orgs[thread.responder_org_id]} gets your words as written. {leave}"
            )
    if not (owner and own_words):
        return None
    return _CardAnswer(card=card, request_id=request.id, goes_to=goes_to)


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
