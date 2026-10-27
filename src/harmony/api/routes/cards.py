"""Cards routes: approve or reject a card in a chat (§5, UI_REFERENCE §4).

- POST /api/cards/{card_id}/approve {recipients?, brief?, asks?}: approve, with edits
- POST /api/cards/{card_id}/reject {feedback}: reject with feedback for Claude

Both run the `ConversationWorkflow.resolve_card` Update with a `CardResolution` built
from the body and the acting member, and return once it's stored: approving the
outreach card creates the request (with the member's brief, if edited) and starts its
`NetworkRequestWorkflow` (`request_id`). Only the conversation's owner may resolve (404
otherwise); a card that is no longer pending, or that its chat sends without review
(`cards.auto`, settled by the workflow), is 409 `card_not_pending`.

Owner: E4.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from temporalio.client import WorkflowUpdateFailedError
from temporalio.service import RPCError

from harmony.agents.models import Ask, Brief, CardResolution, OutreachEdits
from harmony.api.deps import AppServices, ServicesDep
from harmony.api.routes.problems import (
    ApiProblemError,
    from_update_failure,
    invalid,
    not_found,
    problem,
    problems,
    temporal_of,
    workflow_missing,
)
from harmony.auth.session import Actor, ActorDep
from harmony.db import repo
from harmony.db.models import Card, Conversation
from harmony.domain import cards
from harmony.temporal.activities.network import run_update
from harmony.temporal.workflows.payloads import CardResolved, conversation_workflow_id

router = APIRouter(tags=["cards"])


class ApproveCard(BaseModel):
    # Org ids: a subset of the card's proposed recipients. The web always sends them;
    # omitted, the request goes to the suggested ones (`requests.default_recipients`).
    recipients: list[UUID] | None = Field(default=None, min_length=1)
    # The member's rewrite of the brief. Omitted sends Claude's draft as it stands.
    brief: Brief | None = None
    # The member's rewrites of recipients' asks, by org id; the others stand.
    asks: dict[UUID, Ask] | None = None


class RejectCard(BaseModel):
    feedback: str = Field(min_length=1, max_length=4000)


_ERRORS = (status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT)


@router.post("/cards/{card_id}/approve", response_model=CardResolved, responses=problems(*_ERRORS))
async def approve_card(
    card_id: UUID, body: ApproveCard, actor: ActorDep, services: ServicesDep
) -> CardResolved:
    card, conversation = await _pending_card(services, actor, card_id)
    edits: OutreachEdits | None = None
    if body.recipients is not None or body.brief is not None or body.asks is not None:
        proposed = {recipient.org_id for recipient in cards.payload_of(card).recipients}
        if body.recipients is not None and not set(body.recipients) <= proposed:
            raise invalid("recipients must be a subset of the proposed ones")
        if body.asks is not None and not set(body.asks) <= proposed:
            raise invalid("asks must be for proposed recipients")
        edits = OutreachEdits(recipients=body.recipients, brief=body.brief, asks=body.asks)
    resolution = CardResolution(
        card_id=card.id, decision="approve", member_id=actor.member_id, edits=edits
    )
    return await _resolve(services, actor, conversation, resolution)


@router.post("/cards/{card_id}/reject", response_model=CardResolved, responses=problems(*_ERRORS))
async def reject_card(
    card_id: UUID, body: RejectCard, actor: ActorDep, services: ServicesDep
) -> CardResolved:
    card, conversation = await _pending_card(services, actor, card_id)
    resolution = CardResolution(
        card_id=card.id, decision="reject", member_id=actor.member_id, feedback=body.feedback
    )
    return await _resolve(services, actor, conversation, resolution)


async def _pending_card(
    services: AppServices, actor: Actor, card_id: UUID
) -> tuple[Card, Conversation]:
    """The card, in a chat the actor owns (404 otherwise), still pending (409)."""
    async with services.sessionmaker() as db:
        card = await repo.get(db, Card, actor.world_id, card_id)
        conversation = (
            await repo.get(db, Conversation, actor.world_id, card.conversation_id)
            if card is not None
            else None
        )
    if card is None or conversation is None or conversation.owner_member_id != actor.member_id:
        raise not_found("card")
    if card.status != "pending":
        raise _not_pending(card.id, card.status)
    if card.auto:
        raise _not_pending(card.id, "being sent without review")
    return card, conversation


async def _resolve(
    services: AppServices, actor: Actor, conversation: Conversation, resolution: CardResolution
) -> CardResolved:
    handle = temporal_of(services).get_workflow_handle(
        conversation_workflow_id(actor.world_id, conversation.id)
    )
    try:
        return await run_update(handle, "resolve_card", resolution, CardResolved)
    except WorkflowUpdateFailedError as failed:
        raise await from_update_failure(failed, services.sessionmaker, actor.world_id) from None
    except RPCError as error:
        if workflow_missing(error):
            raise _not_pending(resolution.card_id, "no longer awaited") from None
        raise


def _not_pending(card_id: UUID, state: str) -> ApiProblemError:
    return problem(
        status.HTTP_409_CONFLICT, "card_not_pending", f"card {card_id} is {state}, not pending"
    )
