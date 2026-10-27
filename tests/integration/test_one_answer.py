"""One answer, many askers: the matcher puts a requester-side question on an earlier
one's card; a member's answer goes to the card's other askers as reused drafts (checked
in every mode, released by `auto`, or held with why), and the chat shows the card as
one question. Also the resolve route's refusals for a card's answer. Scripted model
calls on a seeded harmony-demo world."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final, cast
from uuid import UUID, uuid4

from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from temporalio.client import Client
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment

from harmony.agents import consult as answerer
from harmony.agents import keys
from harmony.agents.consult import escalation_id_for
from harmony.agents.models import EscalationResolution, InlineQuestionItem, Route, RouteAssignee
from harmony.api.app import create_app
from harmony.config import Settings
from harmony.db import repo
from harmony.db.models import Consult, Conversation, Escalation
from harmony.db.uow import unit_of_work
from harmony.domain import consults, conversations, escalations, routing
from harmony.events.bus import StreamMessage
from harmony.llm import ModelRole
from harmony.llm.budget import FreeSpend
from harmony.llm.scripted import CassetteEntry, CassetteKey, CassetteMatch, ScriptedModel
from harmony.llm.wire import canonical_json
from harmony.temporal.activities.agents import (
    AgentActivities,
    AnswerConsultInput,
    ConsultDecision,
    ConsultEscalated,
    ConsultReleased,
)
from harmony.temporal.activities.network import ApplyResolutionInput, NetworkActivities
from tests.integration._e4_world import (
    ALEX,
    CALDER,
    HENDERSON,
    NORTHLAKE,
    RIDGELINE,
    SCENARIO,
    Outreach,
    Sessionmaker,
    approved_outreach,
    open_thread,
    seeded_world,
    sid,
)
from tests.integration._visitors import ADMIN_SECRET, visitor
from tests.unit._turns import say

BAYSIDE: Final = sid("org", "bayside")
TIDEWATER: Final = sid("org", "tidewater")
SLUGS: Final = {
    NORTHLAKE: "northlake",
    CALDER: "calder",
    RIDGELINE: "ridgeline",
    BAYSIDE: "bayside",
    TIDEWATER: "tidewater",
}
# Northlake's question, which no other conversation's answerer may see.
FIRST_QUESTION: Final = "May I tell them what the powders are? They need it for the mounting plan."
ANSWER: Final = (
    "Yes: say sodium layered oxides, the class we gave Summit Glovebox Services. No compositions."
)
HELD: Final = "Ridgeline asks for the dopant fractions too; only Alex can agree to that."


class Silent:
    async def publish(self, message: StreamMessage) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _scripted(
    org: UUID, role: ModelRole, step: int, output: Mapping[str, Any], *, matcher: bool = False
) -> dict[CassetteKey, CassetteEntry]:
    """Henderson's `role` call in its conversation with `org` (its first consult there),
    answering `output` as JSON. The matcher's calls need their exact thread."""
    thread = f"henderson.{SLUGS[org]}"
    if matcher:
        thread = keys.match_key(thread)
    match = CassetteMatch(org="henderson", role=role, thread=thread, turn=1, step=step)
    entry = CassetteEntry(match=match, content=[say(json.dumps(output))])
    return {(SCENARIO, "henderson", role, thread, 1, step): entry}


def _draft(answer: str, held: str | None = None) -> dict[str, Any]:
    return {
        "answer": answer,
        "needs_human": held is not None,
        "needs_human_reason": held,
        "route": {"assignees": ["alex"], "reason": "Alex sent the request."},
        "rationale": "From the records.",
    }


WITHIN: Final = {"within_sharing": True, "reason": "The sample class is shared."}
# Rev 2's answerer steps start a block of `ANSWERER_STEPS` on.
REV_2: Final = keys.answerer_step(2, 0)


def _script() -> dict[CassetteKey, CassetteEntry]:
    first = _draft("Probably yes.", held="What the powders are is Alex's call.")
    script: dict[CassetteKey, CassetteEntry] = {}
    for org in (NORTHLAKE, CALDER, RIDGELINE, BAYSIDE):
        script |= _scripted(org, "answerer", 0, first)
    for org in (CALDER, RIDGELINE, BAYSIDE, TIDEWATER):
        script |= _scripted(org, "screener", 0, {"same_as": 1}, matcher=True)
    fitted = _draft("They're sodium layered oxides; compositions stay with us.")
    script |= _scripted(CALDER, "answerer", REV_2, fitted)
    script |= _scripted(CALDER, "sharing_check", 1, WITHIN)
    script |= _scripted(RIDGELINE, "answerer", REV_2, _draft("Sodium layered oxides.", HELD))
    script |= _scripted(TIDEWATER, "answerer", 0, fitted)
    script |= _scripted(TIDEWATER, "sharing_check", 0, WITHIN)
    return script


async def _ask(
    sm: Sessionmaker, outreach: Outreach, org: UUID, question: str
) -> AnswerConsultInput:
    """Henderson's representative's consult in its conversation with `org`."""
    thread_id = await open_thread(sm, outreach, org)
    consult_id = uuid4()
    async with unit_of_work(sm, outreach.world_id) as uow:
        await consults.open(
            uow,
            consult_id=consult_id,
            thread_id=thread_id,
            side="requester",
            org_id=HENDERSON,
            question=question,
            turn_id=uuid4(),
            tool_use_id=f"toolu_{SLUGS[org]}",
        )
        await uow.commit()
    return AnswerConsultInput(
        world_id=outreach.world_id,
        consult_id=consult_id,
        thread_id=thread_id,
        side="requester",
        org_id=HENDERSON,
        cassette_thread=f"henderson.{SLUGS[org]}",
        consult_turn=1,
    )


async def _answer(
    sm: Sessionmaker, model: ScriptedModel, asked: AnswerConsultInput, **rev: Any
) -> ConsultDecision:
    activities = AgentActivities(sm, model, FreeSpend(), Silent(), model_override=None)
    return await ActivityEnvironment().run(activities.answer_consult, asked.model_copy(update=rev))


def _request(model: ScriptedModel, org: UUID, role: ModelRole, step: int) -> str:
    """What Henderson's `role` call at `step` in the conversation with `org` sent."""
    thread = f"henderson.{SLUGS[org]}"
    [sent] = [
        r.body
        for r in model.requests
        if (r.key.role, r.key.thread, r.key.step_idx) == (role, thread, step)
    ]
    return canonical_json(sent)


async def test_one_answer_reaches_every_asker_it_should(db_sessionmaker: Sessionmaker) -> None:
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    outreach = await approved_outreach(
        sm, world_id, NORTHLAKE, CALDER, RIDGELINE, BAYSIDE, TIDEWATER
    )
    model = ScriptedModel(_script())

    # Four conversations ask Alex the same thing; the matcher puts each on the first's card.
    asked = {
        NORTHLAKE: await _ask(sm, outreach, NORTHLAKE, FIRST_QUESTION),
        CALDER: await _ask(sm, outreach, CALDER, "May I tell them what the powders are?"),
        RIDGELINE: await _ask(sm, outreach, RIDGELINE, "Can I say what the powders are?"),
        BAYSIDE: await _ask(sm, outreach, BAYSIDE, "May we share what the samples are?"),
    }
    for org, consult in asked.items():
        decided = await _answer(sm, model, consult)
        assert isinstance(decided, ConsultEscalated), org
        assert decided.assignee_ids == [ALEX]
    card = asked[NORTHLAKE].consult_id
    async with sm() as session:
        for org in (CALDER, RIDGELINE, BAYSIDE):
            row = await consults.get(session, world_id=world_id, consult_id=asked[org].consult_id)
            assert row.same_as_id == card
        assert [item.id for item in await consults.card_askers(session, world_id, card, ALEX)] == [
            escalation_id_for(asked[org].consult_id) for org in asked
        ]

    # Alex answers Northlake's, ticking every organization but Bayside.
    source = escalation_id_for(card)
    async with unit_of_work(sm, world_id) as uow:
        await consults.set_goes_to(uow, card, [NORTHLAKE, CALDER, RIDGELINE, TIDEWATER])
        await uow.commit()
    resolution = EscalationResolution(
        escalation_id=source, action="approve", text=ANSWER, member_id=ALEX, org_id=HENDERSON
    )
    # Only the database: nothing here reaches Temporal.
    await NetworkActivities(cast(Client, None), sm).apply_resolution(
        ApplyResolutionInput(
            world_id=world_id,
            thread_id=asked[NORTHLAKE].thread_id,
            resolution=resolution,
            message_id=uuid4(),
        )
    )

    # The two other ticked askers each get a rev drafted from it (the `use_answer` Update).
    calder = await _answer(sm, model, asked[CALDER], draft_rev=2, reuse_from=source)
    ridgeline = await _answer(sm, model, asked[RIDGELINE], draft_rev=2, reuse_from=source)

    # Calder's answerer saw Alex's words and date as its first draft would, and nothing of
    # Northlake's question or another organization's name.
    prompt = _request(model, CALDER, "answerer", REV_2)
    assert "Every answer is checked against this description before it's released" in prompt
    assert "# What Alex Chen already told your side" in prompt
    assert "The draft you're revising" not in prompt
    assert "A teammate asked for this redraft" not in prompt
    assert "mounting plan" not in prompt
    assert "Summit" not in prompt
    assert "the class we gave another organization" in prompt
    # The check runs on it although a first draft that needed a person never had one.
    assert answerer.REUSED_CHECK.strip() in _request(model, CALDER, "sharing_check", 1)

    # Calder's went out by `auto`, never as Alex's approval; Ridgeline's waits, saying why.
    assert isinstance(calder, ConsultReleased)
    assert isinstance(ridgeline, ConsultEscalated)
    assert ridgeline.draft_rev == 2
    async with sm() as session:
        row = await consults.get(session, world_id=world_id, consult_id=asked[CALDER].consult_id)
        assert (row.status, row.released_by, row.drafted_from_id) == ("released", "auto", source)
        item = await repo.get_required(session, Escalation, world_id, escalation_id_for(row.id))
        assert (item.status, item.resolved_by_member_id, item.resolution) == (
            "resolved",
            None,
            None,
        )
        item = await repo.get_required(
            session, Escalation, world_id, escalation_id_for(asked[RIDGELINE].consult_id)
        )
        current = await escalations.current_draft(session, item)
        assert item.status == "open"
        assert current is not None
        assert current.held_reason == HELD

    # Tidewater asks later: answered from the card with no one asked.
    asked[TIDEWATER] = await _ask(sm, outreach, TIDEWATER, "Can I tell them what the powders are?")
    later = await _answer(sm, model, asked[TIDEWATER])
    assert isinstance(later, ConsultReleased)
    async with sm() as session:
        row = await consults.get(session, world_id=world_id, consult_id=asked[TIDEWATER].consult_id)
        assert (row.same_as_id, row.drafted_from_id, row.released_by) == (card, source, "auto")
        assert await repo.get(session, Escalation, world_id, escalation_id_for(row.id)) is None

        # Alex's chat shows one question, the answer and where it went.
        chat = await repo.get_required(session, Conversation, world_id, outreach.conversation_id)
        [shown] = [
            item
            for item in await conversations.chat_items(session, chat)
            if isinstance(item, InlineQuestionItem)
        ]
    assert (shown.escalation_id, shown.question, shown.answer) == (source, FIRST_QUESTION, ANSWER)
    assert shown.answered_by is not None
    assert shown.answered_by.slug == "alex"
    assert [(a.from_org.id, a.status) for a in shown.askers] == [
        (CALDER, "resolved"),
        (RIDGELINE, "open"),
        (BAYSIDE, "open"),
    ]
    assert shown.goes_to_org_ids == [NORTHLAKE, CALDER, RIDGELINE, TIDEWATER]
    states = {row.org.id: (row.state, row.reason) for row in shown.deliveries}
    assert states == {
        NORTHLAKE: ("as_written", None),
        CALDER: ("fitted", None),
        RIDGELINE: ("held", HELD),
        BAYSIDE: ("not_sent", None),
        TIDEWATER: ("later", None),
    }

    # Alex approves Bayside's own draft as written. Drafted for Bayside's conversation,
    # it never becomes the card's answer, so a later asker still starts from Alex's words.
    bayside = escalation_id_for(asked[BAYSIDE].consult_id)
    await NetworkActivities(cast(Client, None), sm).apply_resolution(
        ApplyResolutionInput(
            world_id=world_id,
            thread_id=asked[BAYSIDE].thread_id,
            resolution=EscalationResolution(
                escalation_id=bayside,
                action="approve",
                draft_rev=1,
                member_id=ALEX,
                org_id=HENDERSON,
            ),
            message_id=uuid4(),
        )
    )
    async with sm() as session:
        row = await consults.get(session, world_id=world_id, consult_id=asked[TIDEWATER].consult_id)
        reuse = await answerer.card_reuse(session, row)
    assert reuse is not None
    assert (reuse.escalation_id, reuse.answer) == (source, ANSWER)


# --- The resolve route's refusals --------------------------------------------------------


async def _routed(
    sm: Sessionmaker,
    world_id: UUID,
    thread_id: UUID,
    member: str,
    question: str,
    *,
    same_as: UUID | None = None,
) -> tuple[UUID, UUID]:
    """Henderson's `question` on the thread, waiting for `member`: (consult, escalation)."""
    consult_id = uuid4()
    async with unit_of_work(sm, world_id) as uow:
        await consults.open(
            uow,
            consult_id=consult_id,
            thread_id=thread_id,
            side="requester",
            org_id=HENDERSON,
            question=question,
            turn_id=uuid4(),
            tool_use_id=f"toolu_{consult_id.hex[:8]}",
        )
        if same_as is not None:
            await consults.record_same_as(uow, consult_id, same_as)
        escalation, _ = await escalations.open(
            uow,
            escalation_id=escalation_id_for(consult_id),
            org_id=HENDERSON,
            kind="consult",
            dedupe_key=f"consult:{consult_id}",
            thread_id=thread_id,
            side="requester",
            consult_id=consult_id,
        )
        route = Route(assignees=[RouteAssignee(member=member, why="Their request.")])
        await routing.apply_route(uow, escalation, route)
        await uow.commit()
    return consult_id, escalation.id


async def test_a_cards_answer_goes_only_where_it_may(
    app_settings: Settings,
    db_engine: AsyncEngine,
    db_sessionmaker: Sessionmaker,
    temporal_env: WorkflowEnvironment,
) -> None:
    sm = db_sessionmaker
    app: FastAPI = create_app(
        app_settings.model_copy(update={"admin_secret": SecretStr(ADMIN_SECRET)}),
        engine=db_engine,
        temporal=temporal_env.client,
    )
    async with visitor(app) as v:
        await v.start()
        world_id = UUID((await v.session())["world_id"])
        outreach = await approved_outreach(sm, world_id, NORTHLAKE, CALDER, RIDGELINE)
        threads = {org: await open_thread(sm, outreach, org) for org in (NORTHLAKE, CALDER)}
        powders = "May I tell them what the powders are?"
        card, shown = await _routed(sm, world_id, threads[NORTHLAKE], "alex", powders)
        _, asker = await _routed(sm, world_id, threads[CALDER], "alex", powders, same_as=card)
        _, mayas = await _routed(
            sm, world_id, threads[CALDER], "maya", "May I say what they're for?", same_as=card
        )
        _, elsewhere = await _routed(sm, world_id, threads[CALDER], "alex", "Can they ship Monday?")
        await v.act_as("alex")
        path = f"/escalations/{shown}/resolve"
        approve = {"action": "approve", "text": "Sodium layered oxides."}

        refused = [
            # Another member's item on the card, an item on another card, itself.
            {**approve, "also_for": [str(mayas)]},
            {**approve, "also_for": [str(elsewhere)]},
            {**approve, "also_for": [str(shown)]},
            # Only an approval in the member's own words goes to other askers.
            {"action": "decline", "text": "Not yet.", "also_for": [str(asker)]},
            {"action": "approve", "draft_rev": 1, "also_for": [str(asker)]},
            # The ticked list comes only with the answer it governs.
            {"action": "approve", "draft_rev": 1, "goes_to_org_ids": [str(NORTHLAKE)]},
            # Only to organizations the request went to.
            {**approve, "goes_to_org_ids": [str(NORTHLAKE), str(BAYSIDE)]},
            # An answer that names another organization on the request, in full or short.
            {**approve, "text": "Same as we told Calder  computational."},
            {**approve, "text": "Same as we told Ridgeline: sodium layered oxides."},
        ]
        details: list[str] = []
        for body in refused:
            problem = await v.json("POST", path, json=body, expect=422)
            assert problem["code"] == "invalid", body
            details.append(problem["detail"])
        assert details[-2].startswith("Your answer names Calder Computational,")
        assert details[-1].startswith("Your answer names Ridgeline Fabrication,")

        # Only the chat's owner sets where the answer goes, and only from an item routed
        # to them: not Alex on Maya's item, nor Maya on her own.
        ticked = {**approve, "goes_to_org_ids": [str(NORTHLAKE)]}
        for member in ("alex", "maya"):
            await v.act_as(member)
            problem = await v.json("POST", f"/escalations/{mayas}/resolve", json=ticked, expect=422)
            assert problem["code"] == "invalid", member
        await v.act_as("alex")

        # The asker's own organization isn't another one; with no workflow running, the
        # item isn't open any more.
        own = {**approve, "text": "Tell Northlake Characterization Core: sodium layered oxides."}
        problem = await v.json("POST", path, json=own, expect=409)
        assert problem["code"] == "escalation_not_open"
    async with sm() as session:
        root = await repo.get_required(session, Consult, world_id, card)
        assert root.goes_to_org_ids == []
