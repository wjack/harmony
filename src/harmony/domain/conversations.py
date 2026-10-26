"""Conversations: internal Claude chats and their view model (§5, §12).

The wire transcript (`chat_messages`) is written only by the `internal_turn` activity,
in turn order, so a member message never lands between a request and its response
(that would break the byte-prefix rule, §2.2). The API queues member messages on the
`ConversationWorkflow` instead, and the turn appends them first.

`chat_items` derives the chat view (`agents.models.ChatItem`) from:
- `chat_messages`: member text → `user_text` (code-written user turns such as tool
  results and "No new message from the member." are hidden); assistant `text` →
  `assistant_text`; summarized `thinking` → `thinking_summary`; each `tool_use` →
  `tool_activity` (status from its `tool_result`), except `send_network_request`, which
  renders as its card;
- `cards` (by `tool_use_id`) → `outreach_card`, followed by a `request_status` item
  once it is approved;
- requester-side consult escalations on this chat's requests that were routed to its
  owner → `inline_question` (R§7), kept once settled or cancelled.
Items are ordered by when they happened, ties by transcript seq.

Every write bumps `conversations.version` and `last_activity_at` and queues a
`conversation` snapshot to the owner, which tells the client to refetch the items.

Owner: E4.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, JsonValue, ValidationError
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents.models import (
    AssistantTextItem,
    CardRecipient,
    ChatItem,
    EscalationResolution,
    InlineQuestionItem,
    OutreachCardItem,
    RequestStatusItem,
    ScreenedCounts,
    ThinkingSummaryItem,
    ToolActivityItem,
    ToolActivityStatus,
    UserTextItem,
)
from harmony.auth.views import OrgRef, org_ref
from harmony.db import repo
from harmony.db.models import (
    Card,
    ChatMessage,
    Consult,
    Conversation,
    Escalation,
    NetworkRequest,
    Org,
    Thread,
)
from harmony.db.uow import UnitOfWork
from harmony.domain import cards as cards_domain
from harmony.domain import escalations, records
from harmony.events.schemas import EntitySnapshot, EventScope
from harmony.llm.wire import canonical_json

TITLE_MAX_CHARS = 80
# The fixed user turn that opens a turn woken by a network update (§2.2).
NO_MEMBER_MESSAGE: Final = "No new message from the member."
CARD_TOOLS: Final = frozenset(("send_network_request",))
_TOOL_LABELS: Final[Mapping[str, tuple[str, str | None]]] = {
    "search_org_knowledge": ("Searched your organization's knowledge", "query"),
    "read_document": ("Read a document", None),
    "search_network": ("Searched the Harmony network", None),
    "get_request_status": ("Checked on the request", None),
}


async def start(
    uow: UnitOfWork,
    *,
    conversation_id: UUID,
    org_id: UUID,
    owner_member_id: UUID,
    first_message: str,
) -> Conversation:
    """Create a conversation titled from its first message (truncated). Idempotent on
    `conversation_id`."""
    existing = await repo.get(uow.session, Conversation, uow.world_id, conversation_id)
    if existing is not None:
        return existing
    conversation = Conversation(
        world_id=uow.world_id,
        id=conversation_id,
        org_id=org_id,
        owner_member_id=owner_member_id,
        title=title_from(first_message),
    )
    uow.session.add(conversation)
    await uow.session.flush()
    _queue(uow, conversation)
    return conversation


async def discard(uow: UnitOfWork, conversation_id: UUID) -> bool:
    """Delete a chat its workflow never reached (starting it failed), so no empty chat
    stays in its owner's Recents, which refetch on the deletion's snapshot. A chat with
    a message is its workflow's, and stays. Returns whether it went."""
    session = uow.session
    conversation = await repo.get(session, Conversation, uow.world_id, conversation_id)
    if conversation is None:
        return False
    reached = await session.scalar(
        select(ChatMessage.id)
        .where(ChatMessage.world_id == uow.world_id, ChatMessage.conversation_id == conversation_id)
        .limit(1)
    )
    if reached is not None:
        return False
    scope = EventScope.member(conversation.owner_member_id, conversation.org_id)
    version = conversation.version
    await session.delete(conversation)
    uow.add_event(
        EntitySnapshot(
            entity_type=Conversation.entity_type,
            entity_id=conversation_id,
            version=version + 1,
            data={"id": str(conversation_id), "deleted": True},
        ),
        scope,
    )
    return True


def title_from(text: str) -> str | None:
    """The first message on one line, cut at a word boundary to `TITLE_MAX_CHARS`."""
    line = " ".join(text.split())
    if len(line) <= TITLE_MAX_CHARS:
        return line or None
    cut = line[: TITLE_MAX_CHARS - 1]
    head, space, _ = cut.rpartition(" ")
    return (head if space and len(head) >= TITLE_MAX_CHARS // 2 else cut).rstrip(" ,.;:") + "…"


async def append_member_message(
    uow: UnitOfWork,
    conversation_id: UUID,
    *,
    message_id: UUID,
    member_id: UUID,
    text: str,
    turn_id: UUID,
) -> ChatMessage:
    """Append a member's queued message as a `user` wire message at the start of the
    turn that drains it. Idempotent on `message_id` (a retried turn reuses the row)."""
    return await _append(
        uow,
        conversation_id,
        message_id=message_id,
        role="user",
        content_text=canonical_json([{"type": "text", "text": text}]),
        turn_id=turn_id,
        author_member_id=member_id,
    )


async def append_assistant_content(
    uow: UnitOfWork, conversation_id: UUID, *, turn_id: UUID, content_text: str
) -> ChatMessage:
    """Append one model response's content (raw wire JSON) as an `assistant` message."""
    return await _append(
        uow,
        conversation_id,
        message_id=None,
        role="assistant",
        content_text=content_text,
        turn_id=turn_id,
    )


async def append_turn_input(
    uow: UnitOfWork,
    conversation_id: UUID,
    *,
    turn_id: UUID,
    role: Literal["user", "system"],
    content_text: str,
) -> ChatMessage:
    """Append a code-written `user` message (tool results, the placeholder user turn)
    or a `system` note, as raw wire JSON."""
    return await _append(
        uow,
        conversation_id,
        message_id=None,
        role=role,
        content_text=content_text,
        turn_id=turn_id,
    )


async def touch(uow: UnitOfWork, conversation_id: UUID) -> Conversation:
    """Bump the version and `last_activity_at` without a new message (a card or an
    inline question changed), so the client refetches the items."""
    conversation = await locked(uow, conversation_id)
    conversation.last_activity_at = datetime.now(UTC)
    await uow.session.flush()
    _queue(uow, conversation)
    return conversation


async def locked(uow: UnitOfWork, conversation_id: UUID) -> Conversation:
    """The conversation, row-locked: serializes its transcript `seq` and card writes."""
    conversation = await uow.session.get(
        Conversation,
        (uow.world_id, conversation_id),
        with_for_update=True,
        populate_existing=True,
    )
    if conversation is None:
        raise repo.NotFoundInWorld(Conversation, uow.world_id, conversation_id)
    return conversation


async def recents(
    session: AsyncSession, *, world_id: UUID, owner_member_id: UUID, limit: int = 30
) -> Sequence[Conversation]:
    """The member's conversations, most recent activity first (sidebar Recents)."""
    return (
        await session.scalars(
            repo.in_world(Conversation, world_id)
            .where(Conversation.owner_member_id == owner_member_id)
            .order_by(Conversation.last_activity_at.desc(), Conversation.id)
            .limit(limit)
        )
    ).all()


async def ordinal(session: AsyncSession, conversation: Conversation) -> int:
    """The conversation's 1-based position among its owner's conversations, by
    `created_at`: the `n` of its cassette key `chat:<member>:<n>`."""
    count = await session.scalar(
        select(func.count()).where(
            Conversation.world_id == conversation.world_id,
            Conversation.owner_member_id == conversation.owner_member_id,
            tuple_(Conversation.created_at, Conversation.id)
            <= tuple_(conversation.created_at, conversation.id),
        )
    )
    return max(int(count or 0), 1)


async def turns_taken(session: AsyncSession, conversation: Conversation) -> int:
    """How many internal turns the chat has run: the cassette turn number continues
    from here when its workflow starts again after an idle exit."""
    count = await session.scalar(
        select(func.count(func.distinct(ChatMessage.turn_id))).where(
            ChatMessage.world_id == conversation.world_id,
            ChatMessage.conversation_id == conversation.id,
        )
    )
    return int(count or 0)


async def requests_of(session: AsyncSession, conversation: Conversation) -> list[NetworkRequest]:
    """The requests sent from this chat, oldest first."""
    return list(
        await session.scalars(
            repo.in_world(NetworkRequest, conversation.world_id)
            .where(NetworkRequest.conversation_id == conversation.id)
            .order_by(NetworkRequest.created_at, NetworkRequest.id)
        )
    )


# --- The chat view model -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Placed:
    at: datetime
    # Transcript items keep their (seq, block) order within one instant; derived items
    # (request status, inline questions) come after them.
    order: tuple[int, int, int]
    item: ChatItem


async def chat_items(session: AsyncSession, conversation: Conversation) -> list[ChatItem]:
    """The chat view model (module docstring)."""
    world_id = conversation.world_id
    rows = list(
        await session.scalars(
            repo.in_world(ChatMessage, world_id)
            .where(ChatMessage.conversation_id == conversation.id)
            .order_by(ChatMessage.seq)
        )
    )
    chat_cards = {
        card.tool_use_id: card
        for card in await cards_domain.for_conversation(
            session, world_id=world_id, conversation_id=conversation.id
        )
    }
    sent = await requests_of(session, conversation)
    request_by_card = {req.card_id: req for req in sent if req.card_id is not None}
    orgs = await _org_refs(session, world_id)
    results = _tool_results(rows)
    read = records.read_ids(_tool_calls(rows))
    titles = await records.titles(session, world_id=world_id, org_id=conversation.org_id, ids=read)

    placed: list[_Placed] = []
    for row in rows:
        for block_idx, block in enumerate(_blocks(row)):
            item = _transcript_item(
                conversation,
                row,
                block_idx,
                block,
                results=results,
                titles=titles,
                chat_cards=chat_cards,
                request_by_card=request_by_card,
                orgs=orgs,
            )
            if item is not None:
                placed.append(_Placed(item.at, (0, row.seq, block_idx), item))

    for index, request in enumerate(sent):
        if request.card_id is not None:
            placed.append(
                _Placed(
                    request.created_at,
                    (1, index, 0),
                    RequestStatusItem(
                        id=f"req:{request.id}", at=request.created_at, request_id=request.id
                    ),
                )
            )
        placed += [
            _Placed(item.at, (2, index, position), item)
            for position, item in enumerate(
                await _inline_questions(session, conversation, request, orgs)
            )
        ]

    placed.sort(key=lambda entry: (entry.at, entry.order))
    return [entry.item for entry in placed]


def _blocks(row: ChatMessage) -> list[Mapping[str, JsonValue]]:
    if row.role == "system":
        return []
    content = json.loads(row.content_text)
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [block for block in content if isinstance(block, dict)]


def _tool_calls(rows: Iterable[ChatMessage]) -> list[tuple[str, JsonValue | None]]:
    calls: list[tuple[str, JsonValue | None]] = []
    for row in rows:
        if row.role != "assistant":
            continue
        for block in _blocks(row):
            name = block.get("name")
            if block.get("type") == "tool_use" and isinstance(name, str):
                calls.append((name, block.get("input")))
    return calls


@dataclass(frozen=True, slots=True)
class _ToolResult:
    status: ToolActivityStatus
    content: JsonValue


def _tool_results(rows: Iterable[ChatMessage]) -> dict[str, _ToolResult]:
    results: dict[str, _ToolResult] = {}
    for row in rows:
        if row.role != "user":
            continue
        for block in _blocks(row):
            tool_use_id = block.get("tool_use_id")
            if block.get("type") == "tool_result" and isinstance(tool_use_id, str):
                status: ToolActivityStatus = "error" if block.get("is_error") is True else "done"
                results[tool_use_id] = _ToolResult(status, block.get("content"))
    return results


def _transcript_item(
    conversation: Conversation,
    row: ChatMessage,
    block_idx: int,
    block: Mapping[str, JsonValue],
    *,
    results: Mapping[str, _ToolResult],
    titles: Mapping[UUID, str],
    chat_cards: Mapping[str, Card],
    request_by_card: Mapping[UUID, NetworkRequest],
    orgs: Mapping[UUID, OrgRef],
) -> ChatItem | None:
    item_id = f"msg:{row.id}:{block_idx}"
    kind = block.get("type")
    text = block.get("text")
    if row.role == "user":
        if kind != "text" or not isinstance(text, str) or text == NO_MEMBER_MESSAGE:
            return None
        return UserTextItem(
            id=item_id,
            at=row.created_at,
            text=text,
            member_id=row.author_member_id or conversation.owner_member_id,
        )
    if kind == "text" and isinstance(text, str) and text.strip():
        return AssistantTextItem(id=item_id, at=row.created_at, markdown=text, turn_id=row.turn_id)
    thinking = block.get("thinking")
    if kind == "thinking" and isinstance(thinking, str) and thinking.strip():
        return ThinkingSummaryItem(
            id=item_id, at=row.created_at, text=thinking, turn_id=row.turn_id
        )
    name, tool_use_id = block.get("name"), block.get("id")
    if kind != "tool_use" or not isinstance(name, str) or not isinstance(tool_use_id, str):
        return None
    if name in CARD_TOOLS:
        card = chat_cards.get(tool_use_id)
        if card is None:
            return None
        return _card_item(card, row.created_at, request_by_card, orgs)
    result = results.get(tool_use_id)
    label, detail = tool_label(
        name, block.get("input"), titles, result=None if result is None else result.content
    )
    return ToolActivityItem(
        id=item_id,
        at=row.created_at,
        tool=name,
        label=label,
        detail=detail,
        status="running" if result is None else result.status,
        turn_id=row.turn_id,
    )


def tool_label(
    name: str,
    tool_input: JsonValue | None,
    titles: Mapping[UUID, str],
    *,
    result: JsonValue = None,
) -> tuple[str, str | None]:
    """A tool call as an activity line: its label, and what details it. For
    `read_document`, the title of the record it read (from `titles`); for
    `search_network`, how many organizations it screened (from its `result`), since the
    need it searched for is internal Claude's own long wording; else the input that
    names it."""
    if name == records.READ_DOCUMENT:
        ref = records.read_ref(tool_input)
        return _TOOL_LABELS[name][0], titles.get(ref) if ref is not None else None
    if name == "search_network":
        return _TOOL_LABELS[name][0], _screened(result)
    label, detail_key = _TOOL_LABELS.get(name, (name.replace("_", " ").capitalize(), None))
    detail = tool_input.get(detail_key) if isinstance(tool_input, dict) and detail_key else None
    return label, detail if isinstance(detail, str) else None


class _SearchResult(BaseModel):
    """The part of `search_network`'s result (a `discovery.Shortlist`) a line reads."""

    screened: ScreenedCounts


def _screened(result: JsonValue) -> str | None:
    if not isinstance(result, str):
        return None
    try:
        total = _SearchResult.model_validate_json(result).screened.total
    except ValidationError:
        return None
    return f"{total} organization" if total == 1 else f"{total} organizations"


def _card_item(
    card: Card,
    at: datetime,
    request_by_card: Mapping[UUID, NetworkRequest],
    orgs: Mapping[UUID, OrgRef],
) -> OutreachCardItem:
    payload = cards_domain.payload_of(card)
    resolution = cards_domain.resolution_of(card)
    request = request_by_card.get(card.id)
    chosen = resolution.edits.recipients if resolution and resolution.edits else None
    return OutreachCardItem(
        id=f"card:{card.id}",
        at=at,
        card_id=card.id,
        status=card.status,
        # Once approved, the brief as the member sent it.
        brief=request.brief if request is not None else payload.brief,
        recipients=[
            CardRecipient(
                org=orgs[recipient.org_id],
                fit=recipient.fit,
                reason=recipient.reason,
                selected=chosen is None or recipient.org_id in chosen,
            )
            for recipient in payload.recipients
            if recipient.org_id in orgs
        ],
        screened=payload.screened,
        feedback=resolution.feedback if resolution and card.status == "rejected" else None,
        request_id=request.id if request is not None else None,
        version=card.version,
    )


async def _inline_questions(
    session: AsyncSession,
    conversation: Conversation,
    request: NetworkRequest,
    orgs: Mapping[UUID, OrgRef],
) -> list[InlineQuestionItem]:
    """The request's consults routed to the chat's owner: questions their side's
    representative asked them, answered from the chat (§7). A settled one shows what the
    member let through, never the representative's tool result."""
    rows = await session.execute(
        select(Escalation, Consult, Thread)
        .join(
            Consult,
            (Consult.world_id == Escalation.world_id) & (Consult.id == Escalation.consult_id),
        )
        .join(
            Thread, (Thread.world_id == Escalation.world_id) & (Thread.id == Escalation.thread_id)
        )
        .where(
            Escalation.world_id == conversation.world_id,
            Escalation.org_id == conversation.org_id,
            Escalation.kind == "consult",
            Escalation.side == "requester",
            Escalation.assignees.contains([str(conversation.owner_member_id)]),
            Thread.request_id == request.id,
        )
        .order_by(Escalation.created_at, Escalation.id)
    )
    items: list[InlineQuestionItem] = []
    for escalation, consult, thread in rows:
        resolution = (
            EscalationResolution.model_validate(escalation.resolution)
            if escalation.resolution is not None
            else None
        )
        items.append(
            InlineQuestionItem(
                id=f"esc:{escalation.id}",
                at=escalation.created_at,
                escalation_id=escalation.id,
                thread_id=thread.id,
                question=consult.question,
                from_org=orgs[thread.responder_org_id],
                status=escalations.escalation_status(escalation.status),
                action=resolution.action if resolution is not None else None,
                answer=await _settled_words(session, escalation, resolution),
            )
        )
    return items


async def _settled_words(
    session: AsyncSession, escalation: Escalation, resolution: EscalationResolution | None
) -> str | None:
    """The member's own words, else (on an approval) the draft rev they approved as
    written."""
    if resolution is None:
        return None
    text = (resolution.text or "").strip()
    if text or resolution.action != "approve":
        return text or None
    draft = await escalations.resolved_draft(session, escalation, resolution)
    return draft.answer if draft is not None else None


async def _org_refs(session: AsyncSession, world_id: UUID) -> dict[UUID, OrgRef]:
    return {org.id: org_ref(org) for org in await repo.list_in_world(session, Org, world_id)}


# --- Writes --------------------------------------------------------------------------


async def _append(
    uow: UnitOfWork,
    conversation_id: UUID,
    *,
    message_id: UUID | None,
    role: Literal["user", "assistant", "system"],
    content_text: str,
    turn_id: UUID,
    author_member_id: UUID | None = None,
) -> ChatMessage:
    session = uow.session
    if message_id is not None:
        existing = await repo.get(session, ChatMessage, uow.world_id, message_id)
        if existing is not None:
            return existing
    conversation = await locked(uow, conversation_id)
    last = await session.scalar(
        select(func.max(ChatMessage.seq)).where(
            ChatMessage.world_id == uow.world_id, ChatMessage.conversation_id == conversation_id
        )
    )
    seq = (last or 0) + 1
    message = ChatMessage(
        world_id=uow.world_id,
        # Code-written rows are named by their turn and position, never minted here.
        id=message_id or uuid5(turn_id, f"chat/{conversation_id}/{seq}"),
        conversation_id=conversation_id,
        seq=seq,
        role=role,
        content_text=content_text,
        author_member_id=author_member_id,
        turn_id=turn_id,
    )
    session.add(message)
    conversation.last_activity_at = datetime.now(UTC)
    await session.flush()
    _queue(uow, conversation)
    return message


def _queue(uow: UnitOfWork, conversation: Conversation) -> None:
    uow.add_snapshot(
        conversation, EventScope.member(conversation.owner_member_id, conversation.org_id)
    )


__all__ = [
    "CARD_TOOLS",
    "NO_MEMBER_MESSAGE",
    "TITLE_MAX_CHARS",
    "append_assistant_content",
    "append_member_message",
    "append_turn_input",
    "chat_items",
    "discard",
    "locked",
    "ordinal",
    "recents",
    "requests_of",
    "start",
    "title_from",
    "tool_label",
    "touch",
    "turns_taken",
]
