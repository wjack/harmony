"""Conversations routes: internal Claude chats (§5, §12, UI_REFERENCE §3–4).

- GET /api/conversations: the acting member's chats, most recent first (Recents)
- POST /api/conversations {text, send_without_confirmation?}: start a chat with its
  first message
- GET /api/conversations/{conversation_id}: the chat view (`ChatItem`s)
- PATCH /api/conversations/{conversation_id} {send_without_confirmation}: the chat's
  send mode (null follows the org's default)
- POST /api/conversations/{conversation_id}/messages {text}: send a message
- POST /api/conversations/{conversation_id}/retry {text?}: send the message Claude
  declined again, as written or edited (`ConversationWorkflow.retry_turn`)

Only the owner sees a conversation (404 for anyone else). The API never calls the
model and never writes the transcript: posting signal-with-starts the
`ConversationWorkflow` (`member_message`) with an id minted here; the turn stores it.
When a card awaits the member, the message rejects it with the text as feedback
(`outcome: card_rejected`); a card the chat sends without review never does. Token
deltas for the chat stream as `stream.delta` with the conversation's id (contract:
events/sse.py, E4). Snapshot responses carry `X-Harmony-Cursor`.

Owner: E4.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import WorkflowUpdateFailedError
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError

from harmony.agents.models import ChatItem, MemberRef
from harmony.api.deps import (
    AppServices,
    ServicesDep,
    SessionmakerDep,
    read_cursor,
    set_cursor,
    snapshot_session,
)
from harmony.api.routes.problems import (
    from_update_failure,
    member_ref,
    not_found,
    problem,
    problems,
    temporal_of,
    world_retired,
)
from harmony.auth.session import Actor, ActorDep
from harmony.db import repo
from harmony.db.models import Card, Conversation
from harmony.db.models.choices import FailureReason
from harmony.db.uow import unit_of_work
from harmony.domain import cards, conversations
from harmony.domain.conversations import SendMode
from harmony.errors import WorldRetired
from harmony.temporal.activities.network import (
    conversation_input,
    run_query,
    run_update,
    workflow_missing,
)
from harmony.temporal.start import start_world_workflow
from harmony.temporal.workflows.conversation import ConversationWorkflow
from harmony.temporal.workflows.payloads import (
    ConversationPending,
    MemberMessage,
    RetryTurn,
    TurnRetried,
    conversation_workflow_id,
)

router = APIRouter(tags=["conversations"])

MESSAGE_MAX_CHARS = 20_000
UNTITLED: Final = "New chat"
# The chat view asks the workflow what it holds; a slow or absent workflow just means
# nothing is queued.
PENDING_QUERY_TIMEOUT: Final = timedelta(seconds=2)


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class ConversationSummary(_View):
    """One Recents row."""

    id: UUID
    title: str
    last_activity_at: datetime
    # A card is waiting on the member.
    has_pending_card: bool
    version: int


class StartConversation(BaseModel):
    text: str = Field(min_length=1, max_length=MESSAGE_MAX_CHARS)
    # The new chat's send mode, from the composer; None follows the org's default.
    send_without_confirmation: bool | None = None


class ConversationUpdate(BaseModel):
    """The key is required: null means "follow the org's default"."""

    model_config = ConfigDict(extra="forbid")

    send_without_confirmation: bool | None


class ConversationCreated(_View):
    id: UUID
    # The first message's id; it appears in the items once the first turn stores it.
    message_id: UUID


class ChatRefusal(_View):
    """Claude declined the chat's last turn (`stop_reason: "refusal"`)."""

    # The model's category for it (`stop_details.category`), when it gave one.
    category: str | None
    # The turn whose member messages it declined (`UserTextItem.turn_id`).
    turn_id: UUID
    # The member may send them again, as written or edited (`POST .../retry`).
    retryable: bool


class ConversationDetail(_View):
    id: UUID
    title: str
    owner: MemberRef
    items: list[ChatItem]
    pending_card_id: UUID | None
    # Queued messages not yet in `items` (the workflow holds them until its next turn).
    queued_message_ids: list[UUID]
    turn_in_flight: bool
    # Claude is checking the news on this chat's requests (an update look): it may
    # write, and the member's messages wait for it like any turn.
    looking: bool
    # Why Claude's last turn failed, if it did and nothing has run since: the chat says
    # why it couldn't reply, so the member can try again.
    turn_failure: FailureReason | None
    # Set when that failure is a refusal.
    refusal: ChatRefusal | None = None
    send_mode: SendMode
    version: int


class PostMessage(BaseModel):
    text: str = Field(min_length=1, max_length=MESSAGE_MAX_CHARS)


class RetryMessage(BaseModel):
    # The member's edit of the declined message; omitted sends it again as written.
    text: str | None = Field(default=None, min_length=1, max_length=MESSAGE_MAX_CHARS)


class MessagesRetried(_View):
    # The messages queued again: the edited one's id is new.
    message_ids: list[UUID]


class MessagePosted(_View):
    message_id: UUID
    # `card_rejected`: a card was pending, and this message rejected it as feedback.
    outcome: Literal["queued", "card_rejected"]
    card_id: UUID | None = None


@router.get(
    "/conversations",
    response_model=list[ConversationSummary],
    responses=problems(cursor=True),
)
async def list_conversations(
    actor: ActorDep, sessionmaker: SessionmakerDep, response: Response
) -> list[ConversationSummary]:
    async with snapshot_session(sessionmaker) as db:
        rows = await conversations.recents(
            db, world_id=actor.world_id, owner_member_id=actor.member_id
        )
        with_cards = set(
            await db.scalars(
                select(Card.conversation_id).where(
                    Card.world_id == actor.world_id,
                    Card.conversation_id.in_([row.id for row in rows]),
                    cards.awaiting_member(),
                )
            )
        )
        cursor = await read_cursor(db, actor.world_id)
    set_cursor(response, cursor)
    return [
        ConversationSummary(
            id=row.id,
            title=row.title or UNTITLED,
            last_activity_at=row.last_activity_at,
            has_pending_card=row.id in with_cards,
            version=row.version,
        )
        for row in rows
    ]


@router.post(
    "/conversations",
    response_model=ConversationCreated,
    responses=problems(status.HTTP_409_CONFLICT),
)
async def start_conversation(
    body: StartConversation,
    actor: ActorDep,
    sessionmaker: SessionmakerDep,
    services: ServicesDep,
) -> ConversationCreated:
    temporal_of(services)
    conversation_id = uuid4()
    try:
        async with unit_of_work(sessionmaker, actor.world_id) as uow:
            conversation = await conversations.start(
                uow,
                conversation_id=conversation_id,
                org_id=actor.org_id,
                owner_member_id=actor.member_id,
                first_message=body.text,
                send_without_confirmation=body.send_without_confirmation,
            )
            await uow.commit()
        try:
            message_id = await _send(services, sessionmaker, actor, conversation_id, body.text)
        except Exception:
            # The first message reached no workflow: take the chat back out of Recents.
            # The client keeps the text to send again.
            async with unit_of_work(sessionmaker, actor.world_id) as uow:
                await conversations.discard(uow, conversation_id)
                await uow.commit()
            raise
    except WorldRetired:
        raise world_retired() from None
    return ConversationCreated(id=conversation.id, message_id=message_id)


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetail,
    responses=problems(status.HTTP_404_NOT_FOUND, cursor=True),
)
async def get_conversation(
    conversation_id: UUID,
    actor: ActorDep,
    sessionmaker: SessionmakerDep,
    services: ServicesDep,
    response: Response,
) -> ConversationDetail:
    async with snapshot_session(sessionmaker) as db:
        conversation = await _owned(db, actor, conversation_id)
        items = await conversations.chat_items(db, conversation)
        card = await cards.pending(db, world_id=actor.world_id, conversation_id=conversation.id)
        owner = await member_ref(db, actor.world_id, conversation.owner_member_id)
        mode = await conversations.send_mode(db, conversation)
        cursor = await read_cursor(db, actor.world_id)
    held = await _pending(services, actor.world_id, conversation_id)
    set_cursor(response, cursor)
    if owner is None:
        raise not_found("conversation")
    return ConversationDetail(
        id=conversation.id,
        title=conversation.title or UNTITLED,
        owner=owner,
        items=items,
        pending_card_id=card.id if card is not None else None,
        queued_message_ids=[m.message_id for m in held.queued_messages] if held else [],
        turn_in_flight=held.turn_in_flight if held else False,
        looking=held.looking if held else False,
        turn_failure=held.turn_failure if held else None,
        refusal=_refusal(held),
        send_mode=mode,
        version=conversation.version,
    )


@router.patch(
    "/conversations/{conversation_id}",
    response_model=SendMode,
    responses=problems(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
)
async def update_conversation(
    conversation_id: UUID,
    body: ConversationUpdate,
    actor: ActorDep,
    sessionmaker: SessionmakerDep,
) -> SendMode:
    """Set the chat's send mode. Only its owner may (404 otherwise)."""
    try:
        async with unit_of_work(sessionmaker, actor.world_id) as uow:
            await _owned(uow.session, actor, conversation_id)
            conversation = await conversations.set_send_mode(
                uow, conversation_id, body.send_without_confirmation
            )
            mode = await conversations.send_mode(uow.session, conversation)
            await uow.commit()
    except WorldRetired:
        raise world_retired() from None
    return mode


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessagePosted,
    responses=problems(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
)
async def post_message(
    conversation_id: UUID,
    body: PostMessage,
    actor: ActorDep,
    sessionmaker: SessionmakerDep,
    services: ServicesDep,
) -> MessagePosted:
    temporal_of(services)
    async with sessionmaker() as db:
        conversation = await _owned(db, actor, conversation_id)
        card = await cards.pending(db, world_id=actor.world_id, conversation_id=conversation.id)
    card_id = card.id if card is not None else None
    try:
        message_id = await _send(
            services, sessionmaker, actor, conversation.id, body.text, card_id=card_id
        )
    except WorldRetired:
        raise world_retired() from None
    # The workflow rejects the card this message answers with its text as feedback
    # (§12); with none pending, the message is the next turn's.
    if card is not None:
        return MessagePosted(message_id=message_id, outcome="card_rejected", card_id=card.id)
    return MessagePosted(message_id=message_id, outcome="queued")


@router.post(
    "/conversations/{conversation_id}/retry",
    response_model=MessagesRetried,
    responses=problems(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
)
async def retry_message(
    conversation_id: UUID,
    body: RetryMessage,
    actor: ActorDep,
    sessionmaker: SessionmakerDep,
    services: ServicesDep,
) -> MessagesRetried:
    """Send the message Claude declined again, or the member's edit of it, as a new turn.
    409 `nothing_to_retry` unless Claude's last turn declined it at its first call and
    nothing has happened since."""
    client = temporal_of(services)
    async with sessionmaker() as db:
        conversation = await _owned(db, actor, conversation_id)
    handle = client.get_workflow_handle(conversation_workflow_id(actor.world_id, conversation.id))
    retry = RetryTurn(member_id=actor.member_id, text=body.text, message_id=uuid4())
    try:
        retried = await run_update(handle, "retry_turn", retry, TurnRetried)
    except WorkflowUpdateFailedError as failed:
        raise await from_update_failure(failed, sessionmaker, actor.world_id) from None
    except RPCError as error:
        if not workflow_missing(error):
            raise
        raise problem(
            status.HTTP_409_CONFLICT, "nothing_to_retry", "there's no declined message to retry"
        ) from None
    return MessagesRetried(message_ids=retried.message_ids)


def _refusal(held: ConversationPending | None) -> ChatRefusal | None:
    refused = held.refused if held else None
    if refused is None:
        return None
    return ChatRefusal(
        category=refused.category, turn_id=refused.turn_id, retryable=refused.retryable
    )


async def _owned(db: AsyncSession, actor: Actor, conversation_id: UUID) -> Conversation:
    """The actor's own conversation; anyone else's is 404."""
    conversation = await repo.get(db, Conversation, actor.world_id, conversation_id)
    if conversation is None or conversation.owner_member_id != actor.member_id:
        raise not_found("conversation")
    return conversation


async def _send(
    services: AppServices,
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: Actor,
    conversation_id: UUID,
    text: str,
    *,
    card_id: UUID | None = None,
) -> UUID:
    """Signal-with-start the chat's workflow with a member message (id minted here),
    naming the card pending when it was sent, which it answers."""
    message = MemberMessage(
        message_id=uuid4(),
        member_id=actor.member_id,
        text=text,
        sent_at=datetime.now(UTC),
        card_id=card_id,
    )
    async with sessionmaker() as db:
        conversation = await repo.get_required(db, Conversation, actor.world_id, conversation_id)
        arg = await conversation_input(db, conversation)
    await start_world_workflow(
        temporal_of(services),
        sessionmaker,
        ConversationWorkflow.run,
        arg,
        world_id=actor.world_id,
        workflow_id=conversation_workflow_id(actor.world_id, conversation_id),
        org_id=actor.org_id,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal="member_message",
        start_signal_args=[message],
    )
    return message.message_id


async def _pending(
    services: AppServices, world_id: UUID, conversation_id: UUID
) -> ConversationPending | None:
    """What the chat's workflow holds; None when it isn't running."""
    if services.temporal is None:
        return None
    handle = services.temporal.get_workflow_handle(
        conversation_workflow_id(world_id, conversation_id)
    )
    try:
        return await run_query(
            handle, "pending", ConversationPending, rpc_timeout=PENDING_QUERY_TIMEOUT
        )
    except RPCError:
        return None
