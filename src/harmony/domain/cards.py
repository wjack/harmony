"""Cards: approval and confirmation cards in a chat (§5, UI_REFERENCE §4).

A card is created by an internal-Claude tool (`send_network_request` → `outreach`) and
pauses that turn. It is resolved once, through the `ConversationWorkflow.resolve_card`
Update: approved (with edits), or rejected with feedback, including when the member
just types a reply while it is pending. A newer card for the same conversation
supersedes an older pending one.

Writes queue a `card` snapshot to the conversation's owner and touch the conversation.

Owner: E4.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents.models import CardPayload, CardResolution
from harmony.db import repo
from harmony.db.models import Card, Conversation
from harmony.db.uow import UnitOfWork
from harmony.domain import conversations
from harmony.domain.errors import CardNotPending
from harmony.events.schemas import EventScope

_PAYLOAD: TypeAdapter[CardPayload] = TypeAdapter(CardPayload)


async def create(
    uow: UnitOfWork,
    *,
    card_id: UUID,
    conversation_id: UUID,
    turn_id: UUID,
    tool_use_id: str,
    payload: CardPayload,
) -> Card:
    """A pending card. Idempotent on `card_id` (`side_effect_id(turn, step, tool_use)`);
    supersedes any other pending card of the conversation."""
    session = uow.session
    existing = await repo.get(session, Card, uow.world_id, card_id)
    if existing is not None:
        return existing
    conversation = await conversations.locked(uow, conversation_id)
    for older in await session.scalars(
        repo.in_world(Card, uow.world_id).where(
            Card.conversation_id == conversation_id, Card.status == "pending"
        )
    ):
        older.status = "superseded"
        _queue(uow, conversation, older)
    card = Card(
        world_id=uow.world_id,
        id=card_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        tool_use_id=tool_use_id,
        kind=payload.kind,
        payload=payload.model_dump(mode="json"),
        status="pending",
    )
    session.add(card)
    await session.flush()
    _queue(uow, conversation, card)
    await conversations.touch(uow, conversation_id)
    return card


async def resolve(uow: UnitOfWork, resolution: CardResolution) -> Card:
    """Approve or reject a pending card, storing the resolution.

    Raises `domain.errors.CardNotPending` if it was already resolved or superseded.
    Approving an outreach card does not create the request; `requests.create_from_card`
    does, in the same unit of work.
    """
    card = await uow.session.get(
        Card, (uow.world_id, resolution.card_id), with_for_update=True, populate_existing=True
    )
    if card is None:
        raise repo.NotFoundInWorld(Card, uow.world_id, resolution.card_id)
    if card.status != "pending":
        raise CardNotPending(card.id, card.status)
    card.status = "approved" if resolution.decision == "approve" else "rejected"
    card.resolution = resolution.model_dump(mode="json")
    await uow.session.flush()
    conversation = await conversations.touch(uow, card.conversation_id)
    _queue(uow, conversation, card)
    return card


async def pending(session: AsyncSession, *, world_id: UUID, conversation_id: UUID) -> Card | None:
    """The conversation's pending card, if any."""
    return await session.scalar(
        repo.in_world(Card, world_id)
        .where(Card.conversation_id == conversation_id, Card.status == "pending")
        .order_by(Card.created_at.desc(), Card.id)
        .limit(1)
    )


async def for_conversation(
    session: AsyncSession, *, world_id: UUID, conversation_id: UUID
) -> list[Card]:
    """Every card of the conversation, oldest first."""
    return list(
        await session.scalars(
            repo.in_world(Card, world_id)
            .where(Card.conversation_id == conversation_id)
            .order_by(Card.created_at, Card.id)
        )
    )


def payload_of(card: Card) -> CardPayload:
    """The card's payload, parsed."""
    return _PAYLOAD.validate_python(card.payload)


def resolution_of(card: Card) -> CardResolution | None:
    """The stored resolution, parsed (None while pending or superseded)."""
    if card.resolution is None:
        return None
    return CardResolution.model_validate(card.resolution)


def _queue(uow: UnitOfWork, conversation: Conversation, card: Card) -> None:
    uow.add_snapshot(card, EventScope.member(conversation.owner_member_id, conversation.org_id))


__all__ = [
    "create",
    "for_conversation",
    "payload_of",
    "pending",
    "resolution_of",
    "resolve",
]
