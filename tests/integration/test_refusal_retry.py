"""Retry of a question's declined step: when Claude declines to draft the answer, or to
check the draft against what the org shares, the item says so (`consult_refusals`), and
a Retry runs that step again as the next rev, on the member's edit of the draft when
they made one. Scripted model calls on a seeded harmony-demo world."""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import select
from temporalio.testing import ActivityEnvironment

from harmony.agents import keys
from harmony.agents.consult import escalation_id_for
from harmony.db.models import EscalationDraft, ModelCall
from harmony.db.uow import unit_of_work
from harmony.domain import consults, refusals
from harmony.domain.refusals import Refusal
from harmony.llm import ModelRole
from harmony.llm.budget import FreeSpend
from harmony.llm.scripted import CassetteEntry, CassetteKey, CassetteMatch, ScriptedModel
from harmony.temporal.activities.agents import AgentActivities, AnswerConsultInput, ConsultDecision
from tests.integration._e4_world import (
    HENDERSON,
    NORTHLAKE,
    SCENARIO,
    Sessionmaker,
    approved_outreach,
    open_thread,
    seeded_world,
)
from tests.integration._worlds import create_world
from tests.unit._turns import say

THREAD: Final = "henderson.northlake"
QUESTION: Final = "May I tell them the lattice designs? They need them for the mounting plan."
DRAFT: Final = {
    "answer": "Yes: here are the cell sizes and strut thicknesses for each coupon.",
    "needs_human": False,
    "needs_human_reason": None,
    "route": {"assignees": ["alex"], "reason": "Alex sent the request."},
    "rationale": "From the records.",
}
EDITED: Final = "They're titanium lattice coupons. The designs stay with us."
REWORDED: Final = "Which coupon class are these, for the mounting plan?"
WITHIN: Final = {"within_sharing": True, "reason": "The coupon class is shared."}


class Silent:
    async def publish(self, message: object) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _entry(role: ModelRole, step: int, **reply: Any) -> dict[CassetteKey, CassetteEntry]:
    match = CassetteMatch(org="henderson", role=role, thread=THREAD, turn=1, step=step)
    entry = CassetteEntry(match=match, **reply)
    return {(SCENARIO, "henderson", role, THREAD, 1, step): entry}


def _says(
    role: ModelRole, step: int, output: Mapping[str, Any]
) -> dict[CassetteKey, CassetteEntry]:
    return _entry(role, step, content=[say(json.dumps(output))])


def _declines(role: ModelRole, step: int, category: str) -> dict[CassetteKey, CassetteEntry]:
    return _entry(
        role,
        step,
        content=[],
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": category},
    )


async def _ask(sm: Sessionmaker) -> AnswerConsultInput:
    world_id = await seeded_world(sm)
    outreach = await approved_outreach(sm, world_id, NORTHLAKE)
    thread_id = await open_thread(sm, outreach, NORTHLAKE)
    consult_id = uuid4()
    async with unit_of_work(sm, world_id) as uow:
        await consults.open(
            uow,
            consult_id=consult_id,
            thread_id=thread_id,
            side="requester",
            org_id=HENDERSON,
            question=QUESTION,
            turn_id=uuid4(),
            tool_use_id="toolu_northlake",
        )
        await uow.commit()
    return AnswerConsultInput(
        world_id=world_id,
        consult_id=consult_id,
        thread_id=thread_id,
        side="requester",
        org_id=HENDERSON,
        cassette_thread=THREAD,
        consult_turn=1,
    )


async def _answer(
    sm: Sessionmaker, model: ScriptedModel, asked: AnswerConsultInput, **rev: Any
) -> ConsultDecision:
    activities = AgentActivities(sm, model, FreeSpend(), Silent(), model_override=None)
    return await ActivityEnvironment().run(activities.answer_consult, asked.model_copy(update=rev))


async def _declined(sm: Sessionmaker, asked: AnswerConsultInput) -> Refusal | None:
    """What the open item says Claude declined, read as the workspace reads it."""
    async with sm() as session:
        drafts = await _drafts(sm, asked)
        current = max(drafts, default=None)
        found = await refusals.consult_refusals(session, [(asked.consult_id, current)])
    return found.get(asked.consult_id)


async def _drafts(sm: Sessionmaker, asked: AnswerConsultInput) -> dict[int, str]:
    async with sm() as session:
        rows = await session.execute(
            select(EscalationDraft.draft_rev, EscalationDraft.answer).where(
                EscalationDraft.escalation_id == escalation_id_for(asked.consult_id)
            )
        )
        return dict(rows.all())


def _sent(model: ScriptedModel, role: ModelRole, step: int) -> list[str]:
    return [
        json.dumps(r.body)
        for r in model.requests
        if (r.key.role, r.key.thread, r.key.step_idx) == (role, THREAD, step)
    ]


async def test_a_declined_check_is_retried_on_the_members_edit(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    model = ScriptedModel(
        _says("answerer", 0, DRAFT)
        | _declines("sharing_check", 0, "bio")
        | _says("sharing_check", 1, WITHIN)
    )
    asked = await _ask(sm)

    # The check of rev 1 refused, so a person reviews the draft, told why.
    await _answer(sm, model, asked)
    assert await _drafts(sm, asked) == {1: DRAFT["answer"]}
    assert await _declined(sm, asked) == Refusal(step="sharing_check", category="bio", draft_rev=1)

    # Retry checks the member's edit as rev 2, without drafting again.
    await _answer(sm, model, asked, draft_rev=2, retry=True, draft_text=EDITED)
    assert _sent(model, "answerer", keys.answerer_step(2, 0)) == []
    [checked] = _sent(model, "sharing_check", 1)
    assert "The designs stay with us." in checked
    assert await _drafts(sm, asked) == {1: DRAFT["answer"], 2: EDITED}
    assert await _declined(sm, asked) is None


async def test_a_declined_answer_is_retried_as_a_new_draft(db_sessionmaker: Sessionmaker) -> None:
    sm = db_sessionmaker
    model = ScriptedModel(
        _declines("answerer", 0, "cyber")
        | _says("answerer", keys.answerer_step(2, 0), DRAFT)
        | _says("sharing_check", 1, WITHIN)
    )
    asked = await _ask(sm)

    await _answer(sm, model, asked)
    assert await _drafts(sm, asked) == {}
    assert await _declined(sm, asked) == Refusal(step="answer", category="cyber", draft_rev=1)

    await _answer(sm, model, asked, draft_rev=2, retry=True)
    assert await _drafts(sm, asked) == {2: DRAFT["answer"]}
    assert await _declined(sm, asked) is None


async def test_a_declined_answer_is_retried_on_the_members_rewording(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    model = ScriptedModel(
        _declines("answerer", 0, "bio")
        | _says("answerer", keys.answerer_step(2, 0), DRAFT)
        | _says("sharing_check", 1, WITHIN)
    )
    asked = await _ask(sm)
    await _answer(sm, model, asked)

    # The answerer drafts on the rewording, which the item shows from then on.
    await _answer(sm, model, asked, draft_rev=2, retry=True, question=REWORDED)
    [drafted] = _sent(model, "answerer", keys.answerer_step(2, 0))
    assert REWORDED in drafted
    assert QUESTION not in drafted
    async with sm() as session:
        row = await consults.get(session, world_id=asked.world_id, consult_id=asked.consult_id)
    assert row.question == REWORDED
    assert await _drafts(sm, asked) == {2: DRAFT["answer"]}


def _call(world_id: UUID, turn_id: UUID, step_idx: int, message: Mapping[str, Any]) -> ModelCall:
    return ModelCall(
        turn_id=turn_id,
        step_idx=step_idx,
        world_id=world_id,
        request_text="{}",
        response_text=json.dumps({"role": "assistant", **message}),
        served_model="scripted",
        usage_json={},
        cost_usd=Decimal(0),
        attempt=1,
    )


REFUSED: Final = {"content": [], "stop_reason": "refusal", "stop_details": {"category": "bio"}}
ANSWERED: Final = {"content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn"}


async def test_only_the_step_an_item_waits_on_counts_as_declined(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await create_world(db_sessionmaker)
    again, rewrite, passed = uuid4(), uuid4(), uuid4()
    async with db_sessionmaker.begin() as session:
        session.add_all(
            [
                # Declined, and declined again on Retry: the newest attempt is the one shown.
                _call(world_id, keys.consult_turn_id(again), keys.answerer_step(1, 0), REFUSED),
                _call(world_id, keys.consult_turn_id(again), keys.answerer_step(2, 0), REFUSED),
                # An Edit's rewrite (rev 2) declined while rev 1 still stands: the steer says so.
                _call(world_id, keys.consult_turn_id(rewrite), keys.answerer_step(1, 0), ANSWERED),
                _call(world_id, keys.consult_turn_id(rewrite), keys.answerer_step(2, 0), REFUSED),
                # The check of rev 1 declined, and its Retry passed as rev 2.
                _call(world_id, keys.sharing_turn_id(passed), 0, REFUSED),
                _call(world_id, keys.sharing_turn_id(passed), 1, ANSWERED),
            ]
        )
    async with db_sessionmaker() as session:
        found = await refusals.consult_refusals(session, [(again, None), (rewrite, 1), (passed, 2)])
    assert found == {again: Refusal(step="answer", category="bio", draft_rev=2)}
