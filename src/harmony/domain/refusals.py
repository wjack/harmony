"""Refusals: model calls that ended `stop_reason: "refusal"`, read back from their
checkpoints (`model_calls`), so a view can say Claude declined a step, and in which
category, without the step storing anything more.

`turn_refusals` reads a representative's turn Claude declined (a `refused_turn` item's
subject): its newest call is the refusal.

`consult_refusals` finds the step an open consult item waits on because Claude declined
it: the answerer, when no draft was ever written (its newest attempt refused), or the
sharing check on the item's current draft. A Retry (`retry_answer`) runs that step again
as the next rev, so the newest calls are the ones that count. A steer's rewrite that
was declined isn't one: the steer reports its own failure.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents import keys
from harmony.db.models import ModelCall
from harmony.llm.wire import as_object, as_str

RefusedStep = Literal["answer", "sharing_check", "turn"]


@dataclass(frozen=True, slots=True)
class Refusal:
    step: RefusedStep
    # `stop_details.category`; None when the API gave none.
    category: str | None
    # The draft rev the declined call worked on: a Retry's rev declined again is newer.
    # None for a turn, which has no drafts.
    draft_rev: int | None


def refusal_category(response_text: str) -> tuple[bool, str | None]:
    """Whether a stored response is a refusal, and its category."""
    message = as_object(json.loads(response_text)) or {}
    if message.get("stop_reason") != "refusal":
        return False, None
    return True, as_str((as_object(message.get("stop_details")) or {}).get("category"))


async def consult_refusals(
    session: AsyncSession, items: Iterable[tuple[UUID, int | None]]
) -> dict[UUID, Refusal]:
    """The declined step of each open consult item, by consult id. `items` are
    (consult id, the item's current draft rev)."""
    current = dict(items)
    turns: dict[UUID, tuple[UUID, RefusedStep]] = {}
    for consult_id in current:
        turns[keys.consult_turn_id(consult_id)] = (consult_id, "answer")
        turns[keys.sharing_turn_id(consult_id)] = (consult_id, "sharing_check")
    if not turns:
        return {}
    # Each checkpoint turn's newest call.
    rows = await session.execute(
        select(ModelCall.turn_id, ModelCall.step_idx, ModelCall.response_text)
        .where(ModelCall.turn_id.in_(turns))
        .ext(distinct_on(ModelCall.turn_id))
        .order_by(ModelCall.turn_id, ModelCall.step_idx.desc())
    )
    found: dict[UUID, Refusal] = {}
    for turn_id, step_idx, response_text in rows:
        consult_id, step = turns[turn_id]
        refused, category = refusal_category(response_text)
        if not refused or not _waits_on(step, step_idx, current[consult_id]):
            continue
        found[consult_id] = Refusal(step=step, category=category, draft_rev=_rev_of(step, step_idx))
    return found


async def turn_refusals(session: AsyncSession, turn_ids: Iterable[UUID]) -> dict[UUID, Refusal]:
    """Each representative's turn that ended in a refusal, by turn id, with its category."""
    wanted = list(turn_ids)
    if not wanted:
        return {}
    rows = await session.execute(
        select(ModelCall.turn_id, ModelCall.response_text)
        .where(ModelCall.turn_id.in_(wanted))
        .ext(distinct_on(ModelCall.turn_id))
        .order_by(ModelCall.turn_id, ModelCall.step_idx.desc())
    )
    found: dict[UUID, Refusal] = {}
    for turn_id, response_text in rows:
        refused, category = refusal_category(response_text)
        if refused:
            found[turn_id] = Refusal(step="turn", category=category, draft_rev=None)
    return found


def _waits_on(step: RefusedStep, step_idx: int, current_rev: int | None) -> bool:
    """Whether a refused call is the step the item waits on: an answerer when the item
    has no draft, or the check of the current draft (checked at step `rev - 1`)."""
    if step == "answer":
        return current_rev is None
    return current_rev is not None and step_idx == current_rev - 1


def _rev_of(step: RefusedStep, step_idx: int) -> int:
    """The draft rev a call at `step_idx` worked on: the answerer gives each rev a block
    of `ANSWERER_STEPS` steps, the sharing check one step."""
    return step_idx // keys.ANSWERER_STEPS + 1 if step == "answer" else step_idx + 1


__all__ = ["Refusal", "RefusedStep", "consult_refusals", "refusal_category", "turn_refusals"]
