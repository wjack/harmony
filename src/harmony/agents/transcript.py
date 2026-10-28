"""Transcripts as raw wire JSON (§2.2): rep sessions (`rep_transcript`) and internal
chats (`chat_messages`).

Rules every implementation keeps:
- Append-only. A stored message is never rewritten; ordering is the explicit per-thread
  (per-side) or per-conversation `seq`, never an id.
- `content_text` is the JSON text of the message's `content` array exactly as it was
  sent (`llm.wire.canonical_json`) or received (the response's `content`, re-serialized
  canonically, which keeps key order and empty thinking blocks with their signatures).
- Nothing is written ahead of a call: notices are flushed into the request, not into
  the stored transcript, until the call they went out with has been stored.
- Temporal passes transcript references (`TranscriptRef`), never transcripts; the
  activity loads the bytes.

A chat's update looks (`ConversationWorkflow`) open with a code-written note
(`look_note`), which also quotes any answer the chat's owner just gave their own
representative that Claude may ask about passing on. A later turn's history keeps a
finished look only as its note and what Claude wrote, and leaves a silent one out
(`chat_history`), so the status results looks read don't pile up in the chat.

Every message carries the turn that wrote it. A turn's messages are contiguous and form
the transcript's tail while the turn runs (the workflow runs one turn per transcript at
a time), and each has a **position**: its index among that turn's messages. Appending
with an explicit `position` is idempotent: a message already stored at that position is
reused when it is byte-identical and refused (`TranscriptConflict`) when it isn't, so a
retried activity never duplicates or rewrites what an earlier attempt stored.

Owner: E1.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import groupby
from typing import Annotated, Final, Literal, Protocol
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.agents.context import quoted
from harmony.db.models import ChatMessage, Conversation, RepTranscript
from harmony.db.models.choices import Side, TranscriptRole
from harmony.db.uow import UnitOfWork, unit_of_work
from harmony.errors import HarmonyError
from harmony.events.schemas import EventScope
from harmony.llm.wire import JsonObject, as_str, canonical_json, objects

# Namespaces the chat_messages id of a turn's message at a position.
_CHAT_ID_PREFIX: Final = "transcript"

# An update look's opening: no counterparty text, and the ids, since
# `get_request_status` without one reads only the chat's latest request.
LOOK_NOTE_PREFIX: Final = "Harmony: there's news on "
_LOOK_HEAD: Final = LOOK_NOTE_PREFIX + "{member}'s {requests} since you last spoke."
_LOOK_NEWS: Final = (
    "Check get_request_status for each. Tell {member} only what changes their picture: a "
    "price, a date, a yes or no, a blocker, a counter-proposal. Say who said it."
)
_LOOK_QUIET: Final = "Otherwise end your turn without writing."
LOOK_NOTE: Final = f"{_LOOK_HEAD} {_LOOK_NEWS} {_LOOK_QUIET}"
# A look's part for an answer the chat's owner gave their representative, on a request
# with other open conversations (`AnsweredQuestion`): the member's words, quoted.
LOOK_ANSWER: Final = (
    "On request {request}, {member} answered a question from your representative in "
    "its {conversations} with {asked}, and the answer went there only:\n{answer}\n"
    "It hasn't gone to your representative's other open {other_conversations} on the "
    "request, with {others}. If a specific part of it would clearly help your "
    "representative there, ask {member} whether you may pass that part on: quote the "
    "exact words, and name the organizations whose conversations they would go to. Ask "
    "once, and pass nothing on unless {member} says yes in this chat."
)
# The note as a stored text block starts, for queries over `content_text`.
LOOK_NOTE_BLOCK: Final = canonical_json({"type": "text", "text": LOOK_NOTE_PREFIX})[:-2]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConversationTranscript(_Model):
    """An internal chat: `chat_messages` rows of one conversation."""

    kind: Literal["conversation"] = "conversation"
    world_id: UUID
    conversation_id: UUID


class RepSideTranscript(_Model):
    """One representative side: `rep_transcript` rows of (thread, side)."""

    kind: Literal["rep"] = "rep"
    world_id: UUID
    thread_id: UUID
    side: Side


TranscriptRef = Annotated[ConversationTranscript | RepSideTranscript, Field(discriminator="kind")]


class TranscriptConflict(HarmonyError):
    """An append that would rewrite, reorder or split stored messages."""


class WireMessage(_Model):
    """One stored message: its role and the raw JSON text of its `content`."""

    role: TranscriptRole
    content_text: str
    # The turn that wrote it, as loaded; ignored on append (the append's turn wins).
    turn_id: UUID | None = None

    @classmethod
    def of(cls, role: TranscriptRole, content: JsonValue) -> WireMessage:
        """A message whose content is serialized the one canonical way."""
        return cls(role=role, content_text=canonical_json(content))

    def content(self) -> JsonValue:
        parsed: JsonValue = json.loads(self.content_text)
        return parsed

    def as_json(self) -> JsonObject:
        """`{"role": ..., "content": [...]}` as the request body carries it."""
        return {"role": self.role, "content": self.content()}

    def same_bytes(self, other: WireMessage) -> bool:
        return self.role == other.role and self.content_text == other.content_text


class TranscriptStore(Protocol):
    """Loads and appends one transcript's raw messages. `run_turn` writes through it."""

    async def load(self, ref: TranscriptRef) -> list[WireMessage]:
        """Every stored message of `ref`, in `seq` order, each with its `turn_id`."""
        ...

    async def append(
        self,
        ref: TranscriptRef,
        messages: Sequence[WireMessage],
        *,
        turn_id: UUID,
        author_member_id: UUID | None = None,
        position: int | None = None,
    ) -> list[int]:
        """Append `messages` for `turn_id` in one transaction; returns their seqs.

        `position` is the index of the first message among the turn's messages (module
        docstring). With it the append is idempotent; without it the messages go after
        the turn's stored ones. `author_member_id` is the member who typed the batch's
        `user` messages (a chat message); other rows never have an author. Conversation
        appends also bump the conversation's version (snapshot event), so the chat view
        refetches its items.
        """
        ...


class SqlTranscriptStore:
    """The Postgres `TranscriptStore`."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def load(self, ref: TranscriptRef) -> list[WireMessage]:
        async with self._sessionmaker() as session:
            return await load_in(session, ref)

    async def append(
        self,
        ref: TranscriptRef,
        messages: Sequence[WireMessage],
        *,
        turn_id: UUID,
        author_member_id: UUID | None = None,
        position: int | None = None,
    ) -> list[int]:
        async with unit_of_work(self._sessionmaker, ref.world_id) as uow:
            seqs = await append_in(
                uow,
                ref,
                messages,
                turn_id=turn_id,
                author_member_id=author_member_id,
                position=position,
            )
            await uow.commit()
        return seqs


async def load_in(session: AsyncSession, ref: TranscriptRef) -> list[WireMessage]:
    """`TranscriptStore.load` on a caller's session."""
    if isinstance(ref, ConversationTranscript):
        query = (
            select(ChatMessage.role, ChatMessage.content_text, ChatMessage.turn_id)
            .where(
                ChatMessage.world_id == ref.world_id,
                ChatMessage.conversation_id == ref.conversation_id,
            )
            .order_by(ChatMessage.seq)
        )
    else:
        query = (
            select(RepTranscript.role, RepTranscript.content_text, RepTranscript.turn_id)
            .where(
                RepTranscript.world_id == ref.world_id,
                RepTranscript.thread_id == ref.thread_id,
                RepTranscript.side == ref.side,
            )
            .order_by(RepTranscript.seq)
        )
    rows = (await session.execute(query)).all()
    return [WireMessage(role=row[0], content_text=row[1], turn_id=row[2]) for row in rows]


async def append_in(
    uow: UnitOfWork,
    ref: TranscriptRef,
    messages: Sequence[WireMessage],
    *,
    turn_id: UUID,
    author_member_id: UUID | None = None,
    position: int | None = None,
) -> list[int]:
    """Append inside a caller's unit of work (for writes that must commit together with
    the transcript, such as a member message and its conversation snapshot). Same
    semantics as `TranscriptStore.append`."""
    if ref.world_id != uow.world_id:
        raise ValueError(f"transcript of world {ref.world_id} appended in {uow.world_id}")
    session = uow.session
    # Serializes appenders of this transcript, so seqs and positions can't race.
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(_lock_key(ref), 0)))
    )
    tail_seq = await _tail_seq(session, ref)
    stored = await _turn_rows(session, ref, turn_id)
    start = len(stored) if position is None else position
    if start > len(stored):
        raise TranscriptConflict(
            f"turn {turn_id} has {len(stored)} stored messages; can't append at {start}"
        )
    seqs: list[int] = []
    added = 0
    for offset, message in enumerate(messages):
        at = start + offset
        if at < len(stored):
            seq, existing = stored[at]
            if not existing.same_bytes(message):
                raise TranscriptConflict(
                    f"turn {turn_id} already stored a different message at position {at}"
                )
            seqs.append(seq)
            continue
        if stored and added == 0 and stored[-1][0] != tail_seq:
            raise TranscriptConflict(f"turn {turn_id} is no longer the transcript's tail")
        tail_seq += 1
        added += 1
        author = author_member_id if message.role == "user" else None
        session.add(_row(ref, message, seq=tail_seq, turn_id=turn_id, at=at, author=author))
        seqs.append(tail_seq)
    if added and isinstance(ref, ConversationTranscript):
        await _touch_conversation(uow, ref)
    await session.flush()
    return seqs


@dataclass(frozen=True, slots=True)
class AnsweredQuestion:
    """An answer the chat's owner gave their own representative, which a look asks them
    about passing on."""

    request_id: UUID
    # The member's words.
    answer: str
    # The organizations of the conversations the answer went to.
    asked: Sequence[str]
    # The request's other open conversations, as (organization name, slug).
    others: Sequence[tuple[str, str]]


def look_note(
    member: str, request_ids: Sequence[UUID], answers: Sequence[AnsweredQuestion] = ()
) -> str:
    """The note an update look opens with, naming the requests with news
    (`request_ids`) and any answers it asks about passing on."""
    named = list(dict.fromkeys([*request_ids, *(answer.request_id for answer in answers)]))
    ids = ", ".join(str(request_id) for request_id in named)
    requests = f"requests {ids}" if len(named) > 1 else f"request {ids}"
    if not answers:
        return LOOK_NOTE.format(member=member, requests=requests)
    head = _LOOK_HEAD.format(member=member, requests=requests)
    if request_ids:
        head += " " + _LOOK_NEWS.format(member=member)
    parts = [
        LOOK_ANSWER.format(
            request=answer.request_id,
            member=member,
            conversations=_conversations(answer.asked),
            asked=_listed(list(answer.asked)),
            other_conversations=_conversations(answer.others),
            answer=quoted("answer", answer.answer, from_=member),
            others=_listed([f"{name} ({slug})" for name, slug in answer.others]),
        )
        for answer in answers
    ]
    return "\n\n".join([head, *parts, _LOOK_QUIET])


def _conversations(orgs: Sequence[object]) -> str:
    return "conversation" if len(orgs) == 1 else "conversations"


def _listed(names: Sequence[str]) -> str:
    """ "A", "A and B", "A, B and C"."""
    return " and ".join(names) if len(names) <= 2 else f"{', '.join(names[:-1])} and {names[-1]}"


def is_look_note(message: WireMessage) -> bool:
    """Whether `message` opens an update look: a user message ending on the note (after
    any results a failed turn left owed)."""
    if message.role != "user":
        return False
    blocks = objects(message.content())
    text = as_str(blocks[-1].get("text")) if blocks and blocks[-1].get("type") == "text" else None
    return text is not None and text.startswith(LOOK_NOTE_PREFIX)


def chat_history(history: Sequence[WireMessage], *, turn_id: UUID) -> list[WireMessage]:
    """A chat's stored messages as turn `turn_id` sends them: every finished update look
    reduced to its note and the text Claude wrote, without its tool calls, results and
    thinking, and a look that wrote nothing left out. `turn_id`'s own messages stay
    whole, a look's included."""
    kept: list[WireMessage] = []
    for turn, group in groupby(history, key=lambda message: message.turn_id):
        messages = list(group)
        if turn == turn_id or not is_look_note(messages[0]):
            kept += messages
            continue
        texts = [
            block
            for message in messages
            if message.role == "assistant"
            for block in objects(message.content())
            if block.get("type") == "text" and (as_str(block.get("text")) or "").strip()
        ]
        if texts:
            written = WireMessage.of("assistant", list[JsonValue](texts))
            kept += [messages[0], written.model_copy(update={"turn_id": turn})]
    return kept


def _lock_key(ref: TranscriptRef) -> str:
    if isinstance(ref, ConversationTranscript):
        return f"transcript:{ref.world_id}:{ref.conversation_id}"
    return f"transcript:{ref.world_id}:{ref.thread_id}:{ref.side}"


async def _tail_seq(session: AsyncSession, ref: TranscriptRef) -> int:
    if isinstance(ref, ConversationTranscript):
        last = await session.scalar(
            select(func.max(ChatMessage.seq)).where(
                ChatMessage.world_id == ref.world_id,
                ChatMessage.conversation_id == ref.conversation_id,
            )
        )
    else:
        last = await session.scalar(
            select(func.max(RepTranscript.seq)).where(
                RepTranscript.world_id == ref.world_id,
                RepTranscript.thread_id == ref.thread_id,
                RepTranscript.side == ref.side,
            )
        )
    return last or 0


async def _turn_rows(
    session: AsyncSession, ref: TranscriptRef, turn_id: UUID
) -> list[tuple[int, WireMessage]]:
    """The turn's stored messages with their seqs, in position order."""
    if isinstance(ref, ConversationTranscript):
        query = (
            select(ChatMessage.seq, ChatMessage.role, ChatMessage.content_text)
            .where(
                ChatMessage.world_id == ref.world_id,
                ChatMessage.conversation_id == ref.conversation_id,
                ChatMessage.turn_id == turn_id,
            )
            .order_by(ChatMessage.seq)
        )
    else:
        query = (
            select(RepTranscript.seq, RepTranscript.role, RepTranscript.content_text)
            .where(
                RepTranscript.world_id == ref.world_id,
                RepTranscript.thread_id == ref.thread_id,
                RepTranscript.side == ref.side,
                RepTranscript.turn_id == turn_id,
            )
            .order_by(RepTranscript.seq)
        )
    rows = (await session.execute(query)).all()
    return [(row[0], WireMessage(role=row[1], content_text=row[2])) for row in rows]


def _row(
    ref: TranscriptRef,
    message: WireMessage,
    *,
    seq: int,
    turn_id: UUID,
    at: int,
    author: UUID | None,
) -> ChatMessage | RepTranscript:
    if isinstance(ref, ConversationTranscript):
        return ChatMessage(
            world_id=ref.world_id,
            id=uuid5(turn_id, f"{_CHAT_ID_PREFIX}/{at}"),
            conversation_id=ref.conversation_id,
            seq=seq,
            role=message.role,
            content_text=message.content_text,
            author_member_id=author,
            turn_id=turn_id,
        )
    return RepTranscript(
        world_id=ref.world_id,
        thread_id=ref.thread_id,
        side=ref.side,
        seq=seq,
        role=message.role,
        content_text=message.content_text,
        turn_id=turn_id,
    )


async def _touch_conversation(uow: UnitOfWork, ref: ConversationTranscript) -> None:
    conversation = await uow.session.get(Conversation, (ref.world_id, ref.conversation_id))
    if conversation is None:
        raise LookupError(f"conversation {ref.conversation_id} not in world {ref.world_id}")
    conversation.last_activity_at = datetime.now(UTC)
    uow.add_snapshot(
        conversation, EventScope.member(conversation.owner_member_id, conversation.org_id)
    )


__all__ = [
    "LOOK_ANSWER",
    "LOOK_NOTE",
    "LOOK_NOTE_BLOCK",
    "LOOK_NOTE_PREFIX",
    "AnsweredQuestion",
    "ConversationTranscript",
    "RepSideTranscript",
    "SqlTranscriptStore",
    "TranscriptConflict",
    "TranscriptRef",
    "TranscriptStore",
    "WireMessage",
    "append_in",
    "chat_history",
    "is_look_note",
    "load_in",
    "look_note",
]
