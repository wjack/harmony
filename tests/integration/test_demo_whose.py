"""`GET /api/demo/whose` (FEEDBACK_R2_PLAN §1.3, §2.6): who can open a chat or thread,
for the not-found page's "Switch to …". Names and orgs only, from the session's own
world; anything else is a 404 like an id that exists nowhere."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from harmony.config import Settings
from tests.integration._e4_world import (
    ALEX,
    HELEN,
    HENDERSON,
    MAYA,
    NORTHLAKE,
    RAJ,
    approved_outreach,
    open_thread,
    start_chat,
)
from tests.integration._visitors import HISTORY, Visitor, build_app, visitor
from tests.integration._worlds import demote_to_member

type Sessionmaker = async_sessionmaker[AsyncSession]
type Json = dict[str, Any]


@pytest.fixture
def app(app_settings: Settings, db_engine: AsyncEngine) -> FastAPI:
    return build_app(app_settings, db_engine)


async def _world(v: Visitor) -> UUID:
    return UUID((await v.start())["world_id"])


def _whose(body: Json) -> list[tuple[UUID, list[UUID]]]:
    """Each org's id with its people's ids, in the order the body gives them."""
    return [
        (UUID(entry["org"]["id"]), [UUID(person["member_id"]) for person in entry["people"]])
        for entry in body["orgs"]
    ]


def _fields(body: Json) -> set[str]:
    """Every key anywhere in the body, so a content field can't hide in a nested one."""
    keys = set(body)
    for entry in body["orgs"]:
        keys |= set(entry) | set(entry["org"])
        for person in entry["people"]:
            keys |= set(person)
    return keys


NAMES_ONLY = {"kind", "orgs", "org", "people", "id", "slug", "name", "member_id"}


async def test_a_chat_names_its_owner_and_nothing_from_it(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        world_id = await _world(v)
        chat_id = await start_chat(db_sessionmaker, world_id, "Find a lab for XRD on 14 powders.")
        await v.act_as("helen")

        body = await v.json("GET", "/demo/whose", params={"chat": str(chat_id)})

    assert body["kind"] == "chat"
    assert _whose(body) == [(HENDERSON, [ALEX])]
    assert body["orgs"][0]["people"][0]["name"] == "Alex Chen"
    assert _fields(body) == NAMES_ONLY
    assert "XRD" not in str(body)


async def test_a_thread_names_who_can_open_it_requester_first_and_at_most_three(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    """The seeded Henderson → Northlake history, routed to Alex and Helen."""
    async with visitor(app) as v:
        world_id = await _world(v)
        await v.act_as("dana")
        params = {"thread": str(HISTORY)}

        # Everyone is an admin: Henderson's two, and one place kept for Northlake.
        body = await v.json("GET", "/demo/whose", params=params)
        assert body["kind"] == "thread"
        assert _whose(body) == [(HENDERSON, [ALEX, MAYA]), (NORTHLAKE, [HELEN])]
        assert _fields(body) == NAMES_ONLY
        assert "Powder XRD" not in str(body)
        assert "closed" not in str(body)

        # A member who isn't an admin opens it only with a thread_access row: Alex
        # (routed) still can, after the admins; Maya can't.
        await demote_to_member(db_sessionmaker, world_id, "alex")
        await demote_to_member(db_sessionmaker, world_id, "maya")
        await demote_to_member(db_sessionmaker, world_id, "helen")
        body = await v.json("GET", "/demo/whose", params=params)
        assert _whose(body) == [(HENDERSON, [ALEX]), (NORTHLAKE, [RAJ, HELEN])]


async def test_ids_outside_the_sessions_world_are_not_found(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as other:
        other_world = await _world(other)
        chat_id = await start_chat(db_sessionmaker, other_world)
        outreach = await approved_outreach(db_sessionmaker, other_world, NORTHLAKE)
        thread_id = await open_thread(db_sessionmaker, outreach, NORTHLAKE)
        await other.json("GET", "/demo/whose", params={"thread": str(thread_id)})

    async with visitor(app) as v:
        await _world(v)
        for params in (
            {"chat": str(chat_id)},
            {"thread": str(thread_id)},
            {"chat": str(uuid4())},
            {"thread": str(uuid4())},
        ):
            problem = await v.json("GET", "/demo/whose", params=params, expect=404)
            assert problem["code"] == "not_found"


async def test_it_takes_exactly_one_id_and_a_session(app: FastAPI) -> None:
    async with visitor(app) as v:
        await v.request("GET", "/demo/whose", params={"chat": str(uuid4())}, expect=401)
        await _world(v)
        await v.request("GET", "/demo/whose", expect=422)
        both = {"chat": str(uuid4()), "thread": str(HISTORY)}
        await v.request("GET", "/demo/whose", params=both, expect=422)
