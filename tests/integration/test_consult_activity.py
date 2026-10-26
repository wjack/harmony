"""What internal Claude looked at, per draft rev (REVISION_UNSTRUCTURED.md §9): every rev
of a consult answers under one checkpoint turn, so a redraft's lines must not repeat
the first draft's. A record it read is named by its title, and only the org's own."""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.agents import keys
from harmony.agents.models import ActivityLine
from harmony.db.models import KnowledgeItem, ModelCall, Org
from harmony.domain import consults
from tests.integration._worlds import create_world


def _call(world_id: UUID, turn_id: UUID, step_idx: int, *content: dict[str, object]) -> ModelCall:
    return ModelCall(
        turn_id=turn_id,
        step_idx=step_idx,
        world_id=world_id,
        request_text="{}",
        response_text=json.dumps({"role": "assistant", "content": list(content)}),
        served_model="scripted",
        usage_json={},
        cost_usd=Decimal(0),
        attempt=1,
    )


def _use(name: str, **tool_input: str) -> dict[str, object]:
    return {"type": "tool_use", "id": f"toolu_{uuid4().hex}", "name": name, "input": tool_input}


async def test_each_draft_rev_lists_only_its_own_tool_calls(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world_id = await create_world(db_sessionmaker)
    org_id = uuid4()
    turn_id = keys.consult_turn_id(uuid4())
    draft: dict[str, object] = {"type": "text", "text": "{}"}
    async with db_sessionmaker.begin() as session:
        session.add_all(
            [
                _call(
                    world_id,
                    turn_id,
                    keys.answerer_step(1, 0),
                    _use("read_document", id="kn:calendar"),
                ),
                _call(world_id, turn_id, keys.answerer_step(1, 1), draft),
                _call(
                    world_id,
                    turn_id,
                    keys.answerer_step(2, 0),
                    _use("search_org_knowledge", query="reserve block"),
                ),
                _call(world_id, turn_id, keys.answerer_step(2, 1), draft),
            ]
        )

    async with db_sessionmaker() as session:

        async def lines(steps: range | None = None) -> list[ActivityLine]:
            return await consults.activity(
                session, world_id=world_id, org_id=org_id, turn_id=turn_id, steps=steps
            )

        whole = await lines()
        first = await lines(keys.answerer_steps(1))
        second = await lines(keys.answerer_steps(2))
        third = await lines(keys.answerer_steps(3))

    assert [line.tool for line in whole] == ["read_document", "search_org_knowledge"]
    assert [line.tool for line in first] == ["read_document"]
    assert [(line.tool, line.detail) for line in second] == [
        ("search_org_knowledge", "reserve block")
    ]
    assert third == []


async def test_a_read_record_is_named_by_title_only_when_it_is_the_orgs_own(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world_id = await create_world(db_sessionmaker)
    ours, theirs = uuid4(), uuid4()
    note, other = uuid4(), uuid4()
    turn_id = keys.consult_turn_id(uuid4())
    async with db_sessionmaker.begin() as session:
        session.add_all(
            [
                Org(world_id=world_id, id=ours, slug="northlake", name="Northlake", kind="lab"),
                Org(world_id=world_id, id=theirs, slug="vantage", name="Vantage", kind="lab"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                KnowledgeItem(
                    world_id=world_id, id=note, org_id=ours, title="Raj's note", body="Wed Nov 4"
                ),
                KnowledgeItem(
                    world_id=world_id, id=other, org_id=theirs, title="Client list", body="-"
                ),
            ]
        )
        await session.flush()
        session.add(
            _call(
                world_id,
                turn_id,
                keys.answerer_step(1, 0),
                _use("read_document", id=f"kn:{note}"),
                _use("read_document", id=f"kn:{other}"),
                _use("read_document", id="not-an-id"),
            )
        )

    async with db_sessionmaker() as session:
        found = await consults.activity(session, world_id=world_id, org_id=ours, turn_id=turn_id)

    assert [(line.label, line.detail) for line in found] == [
        ("Read a document", "Raj's note"),
        ("Read a document", None),
        ("Read a document", None),
    ]
