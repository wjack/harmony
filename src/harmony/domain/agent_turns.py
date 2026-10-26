"""Agent turns: one `agent_turns` row per finished turn, with who ran it, where, what it
cost and how long it took (ARCHITECTURE.md §5, §14). The run report lists them, and the
cost forecasts are re-based on them.

A row is written when its turn ends. Its cost is the sum of the turn's checkpointed
model calls, so a retried attempt, which replays those calls, writes the same total;
its latency is the wall time of the attempt that finished it. Internal and
representative turns are keyed by their own turn id. A consult's answerer and sharing
check each run under one checkpoint turn for every draft rev (`agents.keys`), so each
rev gets its own row (`rev_turn_id`), costed from that rev's steps only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from harmony.db.models import AgentTurn, ModelCall
from harmony.db.models.choices import ModelRole, Side
from harmony.db.uow import UnitOfWork


def rev_turn_id(checkpoint_turn_id: UUID, draft_rev: int) -> UUID:
    """The `agent_turns` id of one draft rev of a consult's answerer or sharing check."""
    return uuid5(checkpoint_turn_id, f"rev/{draft_rev}")


async def record(
    uow: UnitOfWork,
    *,
    turn_id: UUID,
    role: ModelRole,
    org_id: UUID,
    latency_ms: int,
    thread_id: UUID | None = None,
    conversation_id: UUID | None = None,
    side: Side | None = None,
    calls_turn_id: UUID | None = None,
    steps: range | None = None,
) -> None:
    """Write (or, for a retried or resumed turn, rewrite) the turn's row.

    Its cost comes from the model calls checkpointed under `calls_turn_id` (default
    `turn_id`), within `steps` when given."""
    calls = ModelCall.turn_id == (calls_turn_id or turn_id)
    if steps is not None:
        calls = calls & ModelCall.step_idx.between(steps.start, steps.stop - 1)
    cost = select(func.coalesce(func.sum(ModelCall.cost_usd), 0)).where(calls).scalar_subquery()
    statement = insert(AgentTurn).values(
        world_id=uow.world_id,
        id=turn_id,
        role=role,
        org_id=org_id,
        thread_id=thread_id,
        conversation_id=conversation_id,
        side=side,
        cost_usd=cost,
        latency_ms=latency_ms,
        completed_at=datetime.now(UTC),
    )
    excluded = statement.excluded
    await uow.session.execute(
        statement.on_conflict_do_update(
            index_elements=[AgentTurn.world_id, AgentTurn.id],
            set_={
                "cost_usd": excluded.cost_usd,
                "latency_ms": excluded.latency_ms,
                "completed_at": excluded.completed_at,
            },
        )
    )


__all__ = ["record", "rev_turn_id"]
