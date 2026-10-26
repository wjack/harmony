"""E4 test helpers: a seeded harmony-demo world with Alex's chat, an approved outreach
card and its request, threads, and a routed consult escalation. Built through the
domain services, so the rows look as the workflows' activities would write them."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.agents.consult import ConsultDraft, escalation_id_for
from harmony.agents.models import (
    CardResolution,
    OutreachCardPayload,
    OutreachRecipient,
    Route,
)
from harmony.db import repo
from harmony.db.models import NetworkRequest
from harmony.db.uow import unit_of_work
from harmony.domain import cards, consults, conversations, escalations, requests, routing, threads
from harmony.seeds.ids import SeedKind, seed_id
from harmony.seeds.loader import load_world

type Sessionmaker = async_sessionmaker[AsyncSession]

SCENARIO: Final = "harmony-demo"


def sid(kind: SeedKind, key: str) -> UUID:
    return seed_id(SCENARIO, kind, key)


HENDERSON: Final = sid("org", "henderson")
NORTHLAKE: Final = sid("org", "northlake")
RIDGELINE: Final = sid("org", "ridgeline")
CALDER: Final = sid("org", "calder")
ALEX: Final = sid("member", "alex")
MAYA: Final = sid("member", "maya")
HELEN: Final = sid("member", "helen")
RAJ: Final = sid("member", "raj")
BRIEF: Final = (
    "Powder XRD on 14 air-sensitive powders by Nov 6. Up to $2,000; ask Alex before "
    "agreeing to anything."
)


@dataclass(frozen=True)
class Outreach:
    world_id: UUID
    conversation_id: UUID
    card_id: UUID
    request_id: UUID


async def seeded_world(sessionmaker: Sessionmaker) -> UUID:
    return await load_world(sessionmaker, SCENARIO, build_sha="test")


async def start_chat(
    sessionmaker: Sessionmaker, world_id: UUID, text: str = BRIEF, *, owner: UUID = ALEX
) -> UUID:
    conversation_id = uuid4()
    async with unit_of_work(sessionmaker, world_id) as uow:
        await conversations.start(
            uow,
            conversation_id=conversation_id,
            org_id=HENDERSON,
            owner_member_id=owner,
            first_message=text,
        )
        await uow.commit()
    return conversation_id


def outreach_payload(*recipients: UUID) -> OutreachCardPayload:
    return OutreachCardPayload(
        brief=BRIEF,
        recipients=[
            OutreachRecipient(org_id=org_id, fit="strong", reason="Runs air-free XRD.")
            for org_id in recipients
        ],
    )


async def propose(
    sessionmaker: Sessionmaker, world_id: UUID, conversation_id: UUID, *recipients: UUID
) -> UUID:
    card_id, turn_id = uuid4(), uuid4()
    tool_use_id = f"toolu_{card_id.hex[:12]}"
    payload = outreach_payload(*recipients)
    async with unit_of_work(sessionmaker, world_id) as uow:
        # The internal turn's tool call the card answers.
        await conversations.append_assistant_content(
            uow,
            conversation_id,
            turn_id=turn_id,
            content_text=json.dumps(
                [
                    {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": "send_network_request",
                        "input": payload.model_dump(mode="json"),
                    }
                ]
            ),
        )
        await cards.create(
            uow,
            card_id=card_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            tool_use_id=tool_use_id,
            payload=payload,
        )
        await uow.commit()
    return card_id


async def approved_outreach(
    sessionmaker: Sessionmaker, world_id: UUID, *recipients: UUID, owner: UUID = ALEX
) -> Outreach:
    """`owner`'s chat (Alex's by default) with an outreach card they approved, and the
    request it made."""
    conversation_id = await start_chat(sessionmaker, world_id, owner=owner)
    card_id = await propose(sessionmaker, world_id, conversation_id, *recipients)
    request_id = uuid4()
    resolution = CardResolution(card_id=card_id, decision="approve", member_id=owner)
    async with unit_of_work(sessionmaker, world_id) as uow:
        card = await cards.resolve(uow, resolution)
        await requests.create_from_card(uow, card, resolution, request_id=request_id)
        await uow.commit()
    return Outreach(world_id, conversation_id, card_id, request_id)


async def open_thread(sessionmaker: Sessionmaker, outreach: Outreach, responder: UUID) -> UUID:
    thread_id = uuid4()
    async with unit_of_work(sessionmaker, outreach.world_id) as uow:
        request = await repo.get_required(
            uow.session, NetworkRequest, outreach.world_id, outreach.request_id
        )
        await threads.open(
            uow, thread_id=thread_id, request=request, responder_org_id=responder, deadline_at=None
        )
        await uow.commit()
    return thread_id


def consult_draft(answer: str = "Yes: Nov 4 09:00-13:00, air-free, $1,180.") -> ConsultDraft:
    return ConsultDraft(
        answer=answer,
        needs_human=True,
        needs_human_reason="It draws on the internal reserve block.",
        route=Route(assignees=["helen"], reason="Helen owns the XRD schedule."),
        rationale="The public calendar starts Nov 10; the reserve block is internal.",
    )


async def routed_consult(
    sessionmaker: Sessionmaker,
    world_id: UUID,
    thread_id: UUID,
    *,
    question: str = "Can you run 14 powders before Nov 6?",
) -> UUID:
    """A Northlake consult on the thread that needed a human: escalated, drafted and
    routed to Helen (as `answer_consult` leaves it). Returns the escalation id."""
    consult_id = uuid4()
    escalation_id = escalation_id_for(consult_id)
    draft = consult_draft()
    async with unit_of_work(sessionmaker, world_id) as uow:
        await consults.open(
            uow,
            consult_id=consult_id,
            thread_id=thread_id,
            side="responder",
            org_id=NORTHLAKE,
            question=question,
            turn_id=uuid4(),
            tool_use_id="toolu_consult",
        )
        escalation, _ = await escalations.open(
            uow,
            escalation_id=escalation_id,
            org_id=NORTHLAKE,
            kind="consult",
            dedupe_key=f"consult:{consult_id}",
            thread_id=thread_id,
            side="responder",
            consult_id=consult_id,
        )
        await consults.record_draft(uow, consult_id, draft, answer_turn_id=uuid4())
        await escalations.add_draft(uow, escalation_id, draft, turn_id=None, draft_rev=1)
        await consults.mark_needs_human(uow, consult_id)
        await routing.apply_route(uow, escalation, draft.route)
        await uow.commit()
    return escalation_id
