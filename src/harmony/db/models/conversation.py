"""Internal Claude chats: conversations, their messages, and approval cards (§5, §12)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Index, Integer, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from harmony.db.models.base import (
    Base,
    CreatedAt,
    Entity,
    JsonObject,
    VersionedEntity,
    one_of,
    world_fk,
)
from harmony.db.models.choices import CardKind, CardStatus, ChatRole


class Conversation(CreatedAt, VersionedEntity, Base):
    """One internal Claude chat, owned by a member. Its workflow is
    `w:{world}:conv:{id}` (§7)."""

    __tablename__ = "conversations"
    __table_args__ = (
        world_fk("org_id", "orgs"),
        world_fk("owner_member_id", "members"),
        Index(None, "world_id", "owner_member_id"),
    )
    entity_type = "conversation"

    org_id: Mapped[UUID]
    owner_member_id: Mapped[UUID]
    title: Mapped[str | None]
    # Sidebar recents sort on this, never on id.
    last_activity_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ChatMessage(CreatedAt, Entity, Base):
    """One message of a chat transcript, append-only and ordered by `seq` (§2.2).

    `content_text` is the raw wire JSON of the message's content exactly as sent to or
    received from the API, stored as text so the bytes never change (prompt caching
    and preserved thinking depend on it).
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        world_fk("conversation_id", "conversations"),
        world_fk("author_member_id", "members"),
        UniqueConstraint("world_id", "conversation_id", "seq"),
        one_of("role", ChatRole),
    )

    conversation_id: Mapped[UUID]
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[ChatRole]
    content_text: Mapped[str]
    # The member who typed a user message; NULL for code-written and model turns.
    author_member_id: Mapped[UUID | None]
    # The agent turn that produced an assistant message.
    turn_id: Mapped[UUID | None]


class Card(CreatedAt, VersionedEntity, Base):
    """An approval card rendered in a chat: the outreach approval
    (`send_network_request`)."""

    __tablename__ = "cards"
    __table_args__ = (
        world_fk("conversation_id", "conversations"),
        Index(None, "world_id", "conversation_id"),
        one_of("kind", CardKind),
        one_of("status", CardStatus),
    )
    entity_type = "card"

    conversation_id: Mapped[UUID]
    turn_id: Mapped[UUID]
    tool_use_id: Mapped[str]
    kind: Mapped[CardKind]
    payload: Mapped[JsonObject]
    status: Mapped[CardStatus] = mapped_column(server_default=text("'pending'"))
    resolution: Mapped[JsonObject | None]
