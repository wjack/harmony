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
  renders as its card. A tool row reads in the present tense until its result is
  stored ("Searching the Harmony network…"), then in the past with what a search found
  ("2 possible fits of 14") or where a share went; its `detail` is what it worked on
  (the need, the query, the record's title, the words shared);
- `cards` (by `tool_use_id`) → `outreach_card`, followed by a `request_status` item
  once it is approved. A recipient row is `selected` when it is in the request: every
  listed row while the card awaits the member or once it was rejected; the rows the
  request went to once approved; and, for an `auto` card still sending, its
  `auto_send_recipients`;
- requester-side consult escalations on this chat's requests that were routed to its
  owner → `inline_question` (R§7), kept once settled or cancelled: one card per
  question, however many conversations asked it (`consults.card_of`). An answered card
  lists where its answer went (`QuestionDelivery`).
Items are ordered by when they happened, ties by transcript seq. An update look's note
never shows, and a look that wrote nothing leaves no item at all: no tool row, no
thinking.

Every write bumps `conversations.version` and `last_activity_at` and queues a
`conversation` snapshot to the owner, which tells the client to refetch the items.
Changing the chat's send mode (`set_send_mode`) bumps the version but not the
activity time: it isn't something that happened in the chat.

Send mode (3.10): a chat sends Harmony Network Requests without the member's review
when its own `send_without_confirmation` says so, else when its org's
`rep_policy.send_without_confirmation` does (`send_mode`, read live).

Owner: E4.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError
from sqlalchemy import ColumnElement, Select, and_, func, or_, select, true, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents import release
from harmony.agents.models import (
    AssistantTextItem,
    CardPayload,
    CardRecipient,
    CardResolution,
    ChatItem,
    DeliveryState,
    EscalationResolution,
    InlineQuestionItem,
    MemberRef,
    OutreachCardItem,
    OutreachRecipient,
    QuestionAsker,
    QuestionDelivery,
    RequestStatusItem,
    ScreenedCounts,
    ThinkingSummaryItem,
    ToolActivityItem,
    ToolActivityStatus,
    UserTextItem,
)
from harmony.agents.transcript import (
    LOOK_NOTE_BLOCK,
    LOOK_NOTE_PREFIX,
    AnsweredQuestion,
    WireMessage,
    is_look_note,
)
from harmony.auth.views import OrgRef, org_ref
from harmony.db import repo
from harmony.db.models import (
    Card,
    ChatMessage,
    Consult,
    Conversation,
    Escalation,
    Member,
    NetworkRequest,
    Org,
    RepPolicy,
    Thread,
    ThreadMessage,
)
from harmony.db.models.choices import CardRejectedWith
from harmony.db.uow import UnitOfWork
from harmony.domain import cards as cards_domain
from harmony.domain import consults, escalations, records, requests
from harmony.domain.threads import TERMINAL_STATES
from harmony.events.schemas import EntitySnapshot, EventScope
from harmony.llm.wire import canonical_json

TITLE_MAX_CHARS = 80
# The fixed user turn that opens a turn woken by a network update (§2.2).
NO_MEMBER_MESSAGE: Final = "No new message from the member."
CARD_TOOLS: Final = frozenset(("send_network_request",))
# (label, the input key its detail reads). Past tense: consults' "Looked at" lines read
# them too, and those tools have always finished.
_TOOL_LABELS: Final[Mapping[str, tuple[str, str | None]]] = {
    records.SEARCH_ORG_KNOWLEDGE: ("Searched your organization's knowledge", "query"),
    records.READ_DOCUMENT: ("Read a document", None),
    "search_network": ("Searched the Harmony network", "need"),
    "get_request_status": ("Checked on the request", None),
    "share_with_representatives": ("Shared with your representatives", "text"),
}
# A chat's row while its tool runs (no result stored yet).
_RUNNING_LABELS: Final[Mapping[str, str]] = {
    records.SEARCH_ORG_KNOWLEDGE: "Searching your organization's knowledge",
    records.READ_DOCUMENT: "Reading a document",
    "search_network": "Searching the Harmony network…",
    "get_request_status": "Checking on the request",
    "share_with_representatives": "Sharing with your representatives",
}


class SendMode(BaseModel):
    """How a chat sends Harmony Network Requests: without review (`True`) or after the
    member approves the card."""

    model_config = ConfigDict(frozen=True)

    # The chat's own choice; None follows the org's default.
    override: bool | None
    org_default: bool
    effective: bool


async def start(
    uow: UnitOfWork,
    *,
    conversation_id: UUID,
    org_id: UUID,
    owner_member_id: UUID,
    first_message: str,
    send_without_confirmation: bool | None = None,
) -> Conversation:
    """Create a conversation titled from its first message (truncated), with the send
    mode its member chose (None follows the org's). Idempotent on `conversation_id`."""
    existing = await repo.get(uow.session, Conversation, uow.world_id, conversation_id)
    if existing is not None:
        return existing
    conversation = Conversation(
        world_id=uow.world_id,
        id=conversation_id,
        org_id=org_id,
        owner_member_id=owner_member_id,
        title=title_from(first_message),
        send_without_confirmation=send_without_confirmation,
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


async def set_send_mode(
    uow: UnitOfWork, conversation_id: UUID, override: bool | None
) -> Conversation:
    """Set the chat's send-mode override (None follows the org's default). Bumps the
    version and tells the owner, but leaves `last_activity_at`, so Recents keep their
    order."""
    conversation = await locked(uow, conversation_id)
    conversation.send_without_confirmation = override
    await uow.session.flush()
    _queue(uow, conversation)
    return conversation


async def send_mode(session: AsyncSession, conversation: Conversation) -> SendMode:
    """The chat's send mode, its org's default read live."""
    policy = await session.get(RepPolicy, (conversation.world_id, conversation.org_id))
    org_default = policy.send_without_confirmation if policy is not None else False
    override = conversation.send_without_confirmation
    return SendMode(
        override=override,
        org_default=org_default,
        effective=org_default if override is None else override,
    )


async def effective_send_mode(session: AsyncSession, conversation: Conversation) -> bool:
    """Whether the chat sends Harmony Network Requests without the member's review."""
    return (await send_mode(session, conversation)).effective


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
    """How many internal turns the chat has run, its update looks aside: the cassette
    turn number continues from here when its workflow starts again after an idle exit."""
    count = await session.scalar(
        select(func.count(func.distinct(ChatMessage.turn_id))).where(
            ChatMessage.world_id == conversation.world_id,
            ChatMessage.conversation_id == conversation.id,
            ChatMessage.turn_id.not_in(_look_turns(conversation)),
        )
    )
    return int(count or 0)


async def looks_taken(session: AsyncSession, conversation: Conversation) -> int:
    """How many update looks stored anything: the look's cassette turn number continues
    from here."""
    count = await session.scalar(
        select(func.count()).select_from(_look_turns(conversation).distinct().subquery())
    )
    return int(count or 0)


async def last_wrote_at(
    session: AsyncSession, conversation: Conversation, *, besides: UUID
) -> datetime | None:
    """When Claude last wrote to the member in this chat (a text block), in a turn other
    than `besides`."""
    return await session.scalar(
        select(func.max(ChatMessage.created_at)).where(
            ChatMessage.world_id == conversation.world_id,
            ChatMessage.conversation_id == conversation.id,
            ChatMessage.role == "assistant",
            ChatMessage.content_text.contains('{"type":"text","text":"', autoescape=True),
            ChatMessage.turn_id.is_distinct_from(besides),
        )
    )


async def last_looked_at(
    session: AsyncSession, conversation: Conversation, *, besides: UUID
) -> datetime | None:
    """When an update look other than `besides` last stored its note."""
    return await session.scalar(
        select(func.max(ChatMessage.created_at)).where(
            _is_look_note(conversation), ChatMessage.turn_id.is_distinct_from(besides)
        )
    )


async def requests_with_news(
    session: AsyncSession, conversation: Conversation, *, since: datetime | None
) -> list[UUID]:
    """The chat's requests where a counterparty wrote, or a conversation ended, after
    `since` (ever, when None), oldest first."""
    ended = Thread.ended_at > since if since is not None else Thread.ended_at.is_not(None)
    wrote = (
        select(ThreadMessage.id)
        .where(
            ThreadMessage.world_id == Thread.world_id,
            ThreadMessage.thread_id == Thread.id,
            ThreadMessage.from_org_id == Thread.responder_org_id,
            ThreadMessage.kind == "message",
            ThreadMessage.created_at > since if since is not None else true(),
        )
        .exists()
    )
    news = (
        select(Thread.id)
        .where(
            Thread.world_id == NetworkRequest.world_id,
            Thread.request_id == NetworkRequest.id,
            or_(ended, wrote),
        )
        .exists()
    )
    rows = await session.scalars(
        select(NetworkRequest.id)
        .where(
            NetworkRequest.world_id == conversation.world_id,
            NetworkRequest.conversation_id == conversation.id,
            news,
        )
        .order_by(NetworkRequest.created_at, NetworkRequest.id)
    )
    return list(rows)


async def answers_to_pass_on(
    session: AsyncSession, conversation: Conversation, *, since: datetime | None
) -> list[AnsweredQuestion]:
    """The answers the chat's owner gave, in their own words, to their own
    representative's questions on the chat's requests after `since` (ever, when None),
    oldest first, each with the request's other open conversations: those that haven't
    ended and didn't ask the question of its card. An answer with none is left out."""
    rows = await session.execute(
        select(Escalation, Consult, NetworkRequest, Thread.responder_org_id)
        .join(
            Consult,
            (Consult.world_id == Escalation.world_id) & (Consult.id == Escalation.consult_id),
        )
        .join(Thread, (Thread.world_id == Consult.world_id) & (Thread.id == Consult.thread_id))
        .join(
            NetworkRequest,
            (NetworkRequest.world_id == Thread.world_id) & (NetworkRequest.id == Thread.request_id),
        )
        .where(
            Escalation.world_id == conversation.world_id,
            NetworkRequest.conversation_id == conversation.id,
            Consult.side == "requester",
            Escalation.resolved_by_member_id == conversation.owner_member_id,
            Escalation.resolved_at > since if since is not None else true(),
        )
        .order_by(Escalation.resolved_at, Escalation.id)
    )
    orgs = await _org_refs(session, conversation.world_id)
    answers: list[AnsweredQuestion] = []
    for item, consult, request, asker_id in rows:
        words = await escalations.settled_words(session, item)
        if consults.member_answer([item], conversation.owner_member_id) is None or not words:
            continue
        card_id = consults.card_of(consult)
        asked = set(
            await session.scalars(
                select(Consult.thread_id).where(
                    Consult.world_id == conversation.world_id, consults.in_card(card_id)
                )
            )
        )
        others = [
            orgs[thread.responder_org_id]
            for thread in await requests.threads_of(session, request)
            if thread.state not in TERMINAL_STATES and thread.id not in asked
        ]
        if not others:
            continue
        card = await consults.get(session, world_id=conversation.world_id, consult_id=card_id)
        went = [UUID(org_id) for org_id in card.goes_to_org_ids] or [asker_id]
        answers.append(
            AnsweredQuestion(
                request_id=request.id,
                answer=words,
                asked=[orgs[org_id].name for org_id in went],
                others=[(org.name, org.slug) for org in others],
            )
        )
    return answers


def _is_look_note(conversation: Conversation) -> ColumnElement[bool]:
    return and_(
        ChatMessage.world_id == conversation.world_id,
        ChatMessage.conversation_id == conversation.id,
        ChatMessage.role == "user",
        ChatMessage.content_text.contains(LOOK_NOTE_BLOCK, autoescape=True),
    )


def _look_turns(conversation: Conversation) -> Select[UUID | None]:
    """The turn ids of the chat's update looks."""
    return select(ChatMessage.turn_id).where(
        _is_look_note(conversation), ChatMessage.turn_id.is_not(None)
    )


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

    silent = _silent_looks(rows)
    placed: list[_Placed] = []
    for row in rows:
        if row.turn_id in silent:
            continue
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


def _silent_looks(rows: Sequence[ChatMessage]) -> set[UUID]:
    """The update looks, by turn, in which Claude wrote nothing. `rows` are in seq
    order, so a turn's first row is its opening."""
    seen: set[UUID] = set()
    looks: set[UUID] = set()
    wrote: set[UUID] = set()
    for row in rows:
        turn = row.turn_id
        if turn is None:
            continue
        if turn not in seen:
            seen.add(turn)
            if is_look_note(WireMessage(role=row.role, content_text=row.content_text)):
                looks.add(turn)
        if turn in looks and row.role == "assistant" and _writes(row):
            wrote.add(turn)
    return looks - wrote


def _writes(row: ChatMessage) -> bool:
    return any(
        block.get("type") == "text" and str(block.get("text") or "").strip()
        for block in _blocks(row)
    )


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
        if (
            kind != "text"
            or not isinstance(text, str)
            or text == NO_MEMBER_MESSAGE
            or text.startswith(LOOK_NOTE_PREFIX)
        ):
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
    label, detail = tool_label(name, block.get("input"), titles)
    if result is None:
        return ToolActivityItem(
            id=item_id,
            at=row.created_at,
            tool=name,
            label=_RUNNING_LABELS.get(name, label),
            detail=detail,
            status="running",
            outcome=None,
            turn_id=row.turn_id,
        )
    outcome = None
    if result.status == "done":
        outcome = (
            _shared_with(block.get("input"), orgs)
            if name == "share_with_representatives"
            else _outcome(name, result.content)
        )
    return ToolActivityItem(
        id=item_id,
        at=row.created_at,
        tool=name,
        label=label,
        detail=detail,
        status=result.status,
        outcome=outcome,
        turn_id=row.turn_id,
    )


def tool_label(
    name: str, tool_input: JsonValue | None, titles: Mapping[UUID, str]
) -> tuple[str, str | None]:
    """A tool call as a past-tense activity line: its label, and what it worked on. For
    `read_document`, the title of the record it read (from `titles`); else the input
    that names it (the need searched for, the query)."""
    if name == records.READ_DOCUMENT:
        ref = records.read_ref(tool_input)
        return _TOOL_LABELS[name][0], titles.get(ref) if ref is not None else None
    label, detail_key = _TOOL_LABELS.get(name, (name.replace("_", " ").capitalize(), None))
    detail = tool_input.get(detail_key) if isinstance(tool_input, dict) and detail_key else None
    return label, detail if isinstance(detail, str) else None


class _Shortlist(BaseModel):
    """The part of `search_network`'s result (a `discovery.Shortlist`) a row reads."""

    screened: ScreenedCounts


class _Matches(BaseModel):
    """The part of `search_org_knowledge`'s result a row reads."""

    results: list[JsonValue]


def _outcome(name: str, content: JsonValue) -> str | None:
    """What a finished search found, from its stored result; None for other tools, and
    for a result this can't read."""
    if not isinstance(content, str):
        return None
    try:
        match name:
            case "search_network":
                return _fits(_Shortlist.model_validate_json(content).screened)
            case records.SEARCH_ORG_KNOWLEDGE:
                if content.startswith(records.NO_MATCHES):
                    return "No matches"
                found = len(_Matches.model_validate_json(content).results)
                return "1 match" if found == 1 else f"{found} matches"
            case _:
                return None
    except ValidationError:
        return None


def _shared_with(tool_input: JsonValue | None, orgs: Mapping[UUID, OrgRef]) -> str | None:
    """Where a share went: "Northlake Characterization Core and Calder Computational"."""
    slugs = tool_input.get("organizations") if isinstance(tool_input, dict) else None
    if not isinstance(slugs, list):
        return None
    names = {org.slug: org.name for org in orgs.values()}
    shown = [names[slug] for slug in slugs if isinstance(slug, str) and slug in names]
    if not shown:
        return None
    return " and ".join(shown) if len(shown) <= 2 else f"{', '.join(shown[:-1])} and {shown[-1]}"


def _fits(screened: ScreenedCounts) -> str:
    """ "1 strong, 2 possible of 14", "2 possible fits of 14", "No fits among 14"."""
    strong, possible, total = screened.strong, screened.possible, screened.total
    if strong and possible:
        return f"{strong} strong, {possible} possible of {total}"
    if strong or possible:
        count, kind = (strong, "strong") if strong else (possible, "possible")
        return f"{count} {kind} {'fit' if count == 1 else 'fits'} of {total}"
    return f"No fits among {total}" if total else "No fits"


def _card_item(
    card: Card,
    at: datetime,
    request_by_card: Mapping[UUID, NetworkRequest],
    orgs: Mapping[UUID, OrgRef],
) -> OutreachCardItem:
    payload = cards_domain.payload_of(card)
    resolution = cards_domain.resolution_of(card)
    request = request_by_card.get(card.id)
    in_request = _in_request(card, payload, resolution, request)
    return OutreachCardItem(
        id=f"card:{card.id}",
        at=at,
        card_id=card.id,
        status=card.status,
        title=payload.title,
        auto=card.auto,
        # Once approved, the brief as the member sent it.
        brief=request.brief if request is not None else payload.brief,
        recipients=[
            CardRecipient(
                org=orgs[recipient.org_id],
                fit=recipient.fit,
                reason=recipient.reason,
                # Once sent, the ask as the member edited it.
                ask=(
                    in_request[recipient.org_id].ask
                    if in_request is not None and recipient.org_id in in_request
                    else recipient.ask
                ),
                title=recipient.title,
                suggested=recipient.suggested,
                selected=in_request is None or recipient.org_id in in_request,
            )
            for recipient in payload.recipients
            if recipient.org_id in orgs
        ],
        screened=payload.screened,
        feedback=resolution.feedback if resolution and card.status == "rejected" else None,
        rejected_with=_rejected_with(card, resolution),
        request_id=request.id if request is not None else None,
        version=card.version,
    )


def _rejected_with(card: Card, resolution: CardResolution | None) -> CardRejectedWith | None:
    if card.status != "rejected" or card.auto or resolution is None:
        return None
    # A row from before `rejected_with` always carried text, which Claude read as a
    # message (`_card_applied`).
    return resolution.rejected_with or "message"


def _in_request(
    card: Card,
    payload: CardPayload,
    resolution: CardResolution | None,
    request: NetworkRequest | None,
) -> dict[UUID, OutreachRecipient] | None:
    """The card's rows that are in the request (module docstring), by org; None when
    every listed row is."""
    if request is not None:
        rows = requests.recipients_of(request)
    elif card.status == "approved" and resolution is not None:
        rows = requests.chosen_recipients(payload, resolution)
    elif card.status == "pending" and card.auto and payload.auto_send_recipients is not None:
        sending = set(payload.auto_send_recipients)
        rows = [row for row in payload.recipients if row.org_id in sending]
    else:
        return None
    return {row.org_id: row for row in rows}


@dataclass(frozen=True, slots=True)
class _Asked:
    """One requester-side consult on the request, its item if a person was asked, and
    the organization its conversation is with."""

    consult: Consult
    item: Escalation | None
    org_id: UUID


async def _inline_questions(
    session: AsyncSession,
    conversation: Conversation,
    request: NetworkRequest,
    orgs: Mapping[UUID, OrgRef],
) -> list[InlineQuestionItem]:
    """The request's questions routed to the chat's owner: questions their side's
    representative asked them, answered from the chat (§7). One card per question: the
    items of a card (`consults.card_of`) that are routed to the owner show as one, the
    oldest open one first (the asker the answer goes to as written), the others as its
    `askers`. An answered card shows the answer the owner approved and where it went. A
    settled one shows what the member let through, never the representative's tool
    result."""
    rows = await session.execute(
        select(Consult, Escalation, Thread.responder_org_id)
        .join(Thread, (Thread.world_id == Consult.world_id) & (Thread.id == Consult.thread_id))
        .outerjoin(
            Escalation,
            (Escalation.world_id == Consult.world_id) & (Escalation.consult_id == Consult.id),
        )
        .where(
            Consult.world_id == conversation.world_id,
            Consult.org_id == conversation.org_id,
            Consult.side == "requester",
            Thread.request_id == request.id,
        )
        .order_by(Consult.created_at, Consult.id)
    )
    cards: dict[UUID, list[_Asked]] = {}
    for consult, item, org_id in rows:
        cards.setdefault(consults.card_of(consult), []).append(_Asked(consult, item, org_id))
    owner = str(conversation.owner_member_id)
    recipients = [row.org_id for row in requests.recipients_of(request)]
    items: list[InlineQuestionItem] = []
    for card_id, asked in cards.items():
        routed = [a for a in asked if a.item is not None and owner in a.item.assignees]
        if routed:
            items.append(
                await _question_card(
                    session, conversation, card_id, asked, routed, recipients, orgs
                )
            )
    return items


async def _question_card(
    session: AsyncSession,
    conversation: Conversation,
    card_id: UUID,
    asked: Sequence[_Asked],
    routed: Sequence[_Asked],
    recipients: Sequence[UUID],
    orgs: Mapping[UUID, OrgRef],
) -> InlineQuestionItem:
    answer = consults.member_answer(
        (a.item for a in asked if a.item is not None), conversation.owner_member_id
    )
    open_items = [a for a in routed if _item(a).status == "open"]
    shown = next(
        (a for a in asked if answer is not None and a.item is answer),
        open_items[0] if open_items else routed[0],
    )
    item = _item(shown)
    resolution = (
        EscalationResolution.model_validate(item.resolution)
        if item.resolution is not None
        else None
    )
    card = next((a.consult for a in asked if a.consult.id == card_id), shown.consult)
    goes_to = [UUID(org_id) for org_id in card.goes_to_org_ids]
    answered_by = None
    if answer is not None and answer.resolved_by_member_id is not None:
        member = await repo.get_required(
            session, Member, answer.world_id, answer.resolved_by_member_id
        )
        answered_by = MemberRef(id=member.id, slug=member.slug, name=member.name)
    return InlineQuestionItem(
        id=f"esc:{_item(routed[0]).id}",
        at=_item(routed[0]).created_at,
        escalation_id=item.id,
        thread_id=item.thread_id or shown.consult.thread_id,
        question=shown.consult.question,
        from_org=orgs[shown.org_id],
        status=escalations.escalation_status(item.status),
        action=resolution.action if resolution is not None else None,
        answer=await escalations.settled_words(session, item),
        askers=[
            QuestionAsker(
                escalation_id=_item(a).id,
                thread_id=a.consult.thread_id,
                from_org=orgs[a.org_id],
                question=a.consult.question,
                status=escalations.escalation_status(_item(a).status),
            )
            for a in routed
            if a is not shown
        ],
        recipients=[orgs[org_id] for org_id in recipients if org_id in orgs],
        deliveries=(
            []
            if answer is None
            else [
                delivery
                for a in asked
                if (delivery := await _delivery(session, a, answer, routed, goes_to, orgs))
                is not None
            ]
        ),
        answered_by=answered_by,
        answered_at=answer.resolved_at if answer is not None else None,
    )


async def _delivery(
    session: AsyncSession,
    asked: _Asked,
    answer: Escalation,
    routed: Sequence[_Asked],
    goes_to: Sequence[UUID],
    orgs: Mapping[UUID, OrgRef],
) -> QuestionDelivery | None:
    """What became of the card's answer in one conversation (`DeliveryState`); None for
    one it never concerned: a question answered from the records alone, or an item a
    teammate decides. `goes_to`: the organizations of the askers it went to."""
    consult, item = asked.consult, asked.item
    answered_at = answer.resolved_at or answer.created_at

    def row(
        state: DeliveryState, at: datetime | None, reason: str | None = None
    ) -> QuestionDelivery:
        return QuestionDelivery(
            thread_id=consult.thread_id, org=orgs[asked.org_id], state=state, at=at, reason=reason
        )

    if item is None:
        return None
    if item is answer:
        return row("as_written", item.resolved_at)
    if item.status == "resolved" and item.resolved_by_member_id is None:
        return row("fitted", item.resolved_at)
    if asked not in routed:
        return None
    match item.status:
        case "cancelled":
            return row("ended", item.resolved_at)
        case "resolved":
            declined = item.resolution is not None and item.resolution.get("action") == "decline"
            return row("not_sent" if declined else "as_written", item.resolved_at)
        case _:
            pass
    current = await escalations.current_draft(session, item)
    if consult.drafted_from_id is not None:
        held = current.held_reason if current is not None else None
        return row(
            "held", current.created_at if current else None, release.why_needed(consult, held)
        )
    # An asker that asked after the answer was given never got it.
    if asked.org_id not in goes_to or item.created_at > answered_at:
        return row("not_sent", None)
    return row("drafting", None)


def _item(asked: _Asked) -> Escalation:
    assert asked.item is not None
    return asked.item


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
    "SendMode",
    "answers_to_pass_on",
    "append_assistant_content",
    "append_member_message",
    "append_turn_input",
    "chat_items",
    "discard",
    "effective_send_mode",
    "last_looked_at",
    "last_wrote_at",
    "locked",
    "looks_taken",
    "ordinal",
    "recents",
    "requests_of",
    "requests_with_news",
    "send_mode",
    "set_send_mode",
    "start",
    "title_from",
    "tool_label",
    "touch",
    "turns_taken",
]
