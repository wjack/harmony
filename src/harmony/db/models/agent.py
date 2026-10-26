"""Agent turns and the per-call checkpoint that makes a retried turn free (§5, §14)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Index, Integer, text
from sqlalchemy.orm import Mapped, mapped_column

from harmony.db.models.base import (
    Base,
    CreatedAt,
    Entity,
    JsonObject,
    deferred_fk,
    one_of,
    world_fk,
)
from harmony.db.models.choices import ModelRole, Side


class AgentTurn(CreatedAt, Entity, Base):
    """One agent turn: who ran, where, and what it cost.

    A turn runs in a thread (representative, answerer, sharing check), a conversation
    (internal Claude), or neither (synthesizer, screener).
    """

    __tablename__ = "agent_turns"
    __table_args__ = (
        world_fk("org_id", "orgs"),
        world_fk("thread_id", "threads"),
        world_fk("conversation_id", "conversations"),
        Index(None, "world_id", "thread_id"),
        one_of("role", ModelRole),
        one_of("side", Side),
    )

    role: Mapped[ModelRole]
    org_id: Mapped[UUID]
    thread_id: Mapped[UUID | None]
    conversation_id: Mapped[UUID | None]
    side: Mapped[Side | None]
    cost_usd: Mapped[Decimal] = mapped_column(server_default=text("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    completed_at: Mapped[datetime | None]


class ModelCall(CreatedAt, Base):
    """One model call: the activity checkpoint and the per-call ledger (§5, §14).

    Keyed by `(turn_id, step_idx)`, not by world: the turn id is globally unique, and
    `call_model_checkpointed` looks the row up by exactly that key before calling. It
    is inserted with `ON CONFLICT DO NOTHING` in its own transaction, before the turn's
    `agent_turns` row exists, so `turn_id` has no foreign key.

    `request_text` and `response_text` are the raw JSON exactly as sent and received:
    text, never jsonb, which would reorder keys and drop the bytes the next request
    must repeat.
    """

    __tablename__ = "model_calls"
    __table_args__ = (Index(None, "world_id"),)

    turn_id: Mapped[UUID] = mapped_column(primary_key=True)
    step_idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    world_id: Mapped[UUID] = mapped_column(deferred_fk("worlds.id", ondelete="CASCADE"))
    request_text: Mapped[str]
    response_text: Mapped[str]
    served_model: Mapped[str]
    # The raw usage object, `iterations` included.
    usage_json: Mapped[JsonObject]
    cost_usd: Mapped[Decimal]
    attempt: Mapped[int] = mapped_column(Integer)
