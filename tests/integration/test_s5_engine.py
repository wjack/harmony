"""Section 5 live-run engine fixes that need the database."""

from __future__ import annotations

from typing import cast
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from harmony.agents.models import CardResolution, OutreachEdits
from harmony.agents.tools.internal import ASK_EDITED, BRIEF_EDITED, NOT_SENT_TO
from harmony.temporal.activities.network import CardResolutionInput, NetworkActivities
from tests.integration._e4_world import (
    ALEX,
    CALDER,
    NORTHLAKE,
    propose,
    seeded_world,
    start_chat,
)

type Sessionmaker = async_sessionmaker[AsyncSession]


async def test_the_card_result_says_what_the_member_changed(
    db_sessionmaker: Sessionmaker,
) -> None:
    """Bug 5: the result read as Claude's own proposal, so Claude misreported what went
    out. It names who was left off, the brief as the member rewrote it, and each
    rewritten ask."""
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    conversation_id = await start_chat(sm, world_id)
    card_id = await propose(sm, world_id, conversation_id, NORTHLAKE, CALDER)
    brief = "Powder XRD on 14 powders by Nov 13. Ask Alex before agreeing to a price."
    ask = "Can you run 14 powders in a glovebox by Nov 13?"
    edits = OutreachEdits(recipients=[NORTHLAKE], brief=brief, asks={NORTHLAKE: ask})

    applied = await NetworkActivities(cast(Client, None), sm).apply_card_resolution(
        CardResolutionInput(
            world_id=world_id,
            conversation_id=conversation_id,
            resolution=CardResolution(
                card_id=card_id, decision="approve", member_id=ALEX, edits=edits
            ),
            request_id=uuid4(),
        )
    )

    parts = applied.content.split("\n\n")
    assert parts[1:6] == [
        NOT_SENT_TO.format(orgs="Calder Computational"),
        BRIEF_EDITED.format(member="Alex Chen"),
        brief,
        ASK_EDITED.format(member="Alex Chen", org="Northlake Characterization Core"),
        ask,
    ]
