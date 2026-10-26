"""Who sees which thread (ARCHITECTURE.md §10, §11.3), through the API and the SQL
predicate, against the harmony-demo access fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from harmony.auth.access import can_see_thread, visible_threads
from harmony.config import Settings
from harmony.db.models import Event, Member, Thread, ThreadAccess, ThreadMessage
from harmony.db.models.choices import AccessReason, MemberRole
from harmony.seeds.loader import load_scenario, load_world
from tests.integration._visitors import HISTORY, RAJ_THREAD, Visitor, build_app, cursor_of, visitor

type Sessionmaker = async_sessionmaker[AsyncSession]

# What each demo member's inbox holds, and why (P0_PLAN.md T3's access fixtures).
INBOXES: Final[dict[str, dict[UUID, list[AccessReason]]]] = {
    "alex": {HISTORY: ["routed"]},
    "maya": {HISTORY: ["admin"]},
    "helen": {HISTORY: ["routed", "admin"], RAJ_THREAD: ["admin"]},
    "raj": {RAJ_THREAD: ["routed"]},
    "dana": {RAJ_THREAD: ["admin"]},
    "priya": {},
    "jordan": {},
}


@pytest.fixture
def app(app_settings: Settings, db_engine: AsyncEngine) -> FastAPI:
    return build_app(app_settings, db_engine)


async def _reasons(v: Visitor) -> dict[UUID, list[AccessReason]]:
    return {thread_id: row["access_reasons"] for thread_id, row in (await v.inbox()).items()}


async def _all_inboxes(v: Visitor) -> dict[str, dict[UUID, list[AccessReason]]]:
    inboxes: dict[str, dict[UUID, list[AccessReason]]] = {}
    for slug in INBOXES:
        await v.act_as(slug)
        inboxes[slug] = await _reasons(v)
    return inboxes


# --- Inboxes and 404s -----------------------------------------------------------------


@pytest.mark.parametrize("slug", list(INBOXES))
async def test_each_member_sees_exactly_their_threads(app: FastAPI, slug: str) -> None:
    async with visitor(app) as v:
        await v.start()
        await v.act_as(slug)
        response = await v.request("GET", "/threads")
        assert cursor_of(response) == 2  # world.created, session.member_changed
        assert await _reasons(v) == INBOXES[slug]

        for thread_id in (HISTORY, RAJ_THREAD, uuid4()):
            expected = 200 if thread_id in INBOXES[slug] else 404
            response = await v.request("GET", f"/threads/{thread_id}", expect=expected)
            if expected == 404:
                # Identical whether the thread is hidden or doesn't exist.
                assert response.json() == {"detail": "thread not found"}


async def test_the_inbox_shows_each_side_its_own_view_of_a_thread(app: FastAPI) -> None:
    async with visitor(app) as v:
        await v.start()
        [summary] = (await v.inbox()).values()
        assert summary["title"] == "Powder XRD on 6 cathode precursor powders within two weeks"
        assert (summary["state"], summary["side"], summary["access_reason"]) == (
            "closed",
            "requester",
            "routed",
        )
        assert (summary["requester"]["slug"], summary["responder"]["slug"]) == (
            "henderson",
            "northlake",
        )
        alex_view = await v.json("GET", f"/threads/{HISTORY}")

        await v.act_as("helen")
        response = await v.request("GET", f"/threads/{HISTORY}")
        helen_view = response.json()
        assert cursor_of(response) == 2

    assert alex_view["side"] == "requester"
    assert helen_view["side"] == "responder"
    threads = load_scenario("harmony-demo").threads
    seeded = next(t for t in threads if t.slug == "henderson-northlake-precursor-xrd")
    assert [m["seq"] for m in alex_view["messages"]] == [1, 2, 3]
    assert [m["kind"] for m in alex_view["messages"]] == [m.kind for m in seeded.messages]
    assert alex_view["messages"] == helen_view["messages"]
    # Each side sees only its own org's access list.
    assert [(a["member_slug"], a["reason"]) for a in alex_view["access"]] == [("alex", "routed")]
    assert [(a["member_slug"], a["reason"]) for a in helen_view["access"]] == [("helen", "routed")]


# --- The rule and its SQL form agree --------------------------------------------------


@dataclass(frozen=True)
class _Viewer:
    world_id: UUID
    member_id: UUID
    org_id: UUID
    role: MemberRole


async def test_the_sql_predicate_matches_the_rule_for_every_member(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await load_world(db_sessionmaker, "harmony-demo", build_sha="test")
    other_world = await load_world(db_sessionmaker, "harmony-demo", build_sha="test")
    async with db_sessionmaker() as session:
        members = list(await session.scalars(select(Member).where(Member.world_id == world_id)))
        threads = list(await session.scalars(select(Thread)))
        rows = list(await session.scalars(select(ThreadAccess)))
        assert {thread.world_id for thread in threads} == {world_id, other_world}

        for member in members:
            viewer = _Viewer(world_id, member.id, member.org_id, member.role)
            by_sql = {thread.id for thread in await session.scalars(visible_threads(viewer))}
            by_rule = {
                thread.id
                for thread in threads
                if can_see_thread(
                    viewer,
                    thread,
                    [
                        row.reason
                        for row in rows
                        if (row.world_id, row.thread_id, row.member_id)
                        == (thread.world_id, thread.id, member.id)
                    ],
                )
            }
            # The network's other orgs' members see none of the seeded threads.
            expected = set(INBOXES.get(member.slug, {}))
            assert by_sql == by_rule == expected, member.slug


# --- Sharing --------------------------------------------------------------------------


async def test_sharing_adds_exactly_one_member(app: FastAPI, db_sessionmaker: Sessionmaker) -> None:
    async with visitor(app) as v:
        started = await v.start()
        raj = (await v.member_ids())["raj"]
        before = await _all_inboxes(v)
        await v.act_as("helen")

        question = "@Raj can you check the precursor batch?"
        shared = await v.json(
            "POST",
            f"/threads/{HISTORY}/share",
            json={"member_id": str(raj), "question": question},
        )
        assert shared == {
            "thread_id": str(HISTORY),
            "member_id": str(raj),
            "reason": "shared",
            "created": True,
        }
        detail = await v.json("GET", f"/threads/{HISTORY}")
        assert [(a["member_slug"], a["reason"], a["question"]) for a in detail["access"]] == [
            ("helen", "routed", None),
            ("raj", "shared", question),
        ]

        again = await v.json("POST", f"/threads/{HISTORY}/share", json={"member_id": str(raj)})
        assert again["created"] is False

        after = await _all_inboxes(v)
        assert after["raj"] == {HISTORY: ["shared"], RAJ_THREAD: ["routed"]}
        assert {slug: inbox for slug, inbox in after.items() if slug != "raj"} == {
            slug: inbox for slug, inbox in before.items() if slug != "raj"
        }

    async with db_sessionmaker() as session:
        events = list(
            await session.scalars(
                select(Event)
                .where(
                    Event.world_id == UUID(started["world_id"]),
                    Event.type == "thread.access_changed",
                )
                .order_by(Event.seq)
            )
        )
    [event] = events
    assert (event.scope_kind, event.member_id, event.thread_id) == ("member", raj, None)
    assert event.payload == {
        "type": "thread.access_changed",
        "thread_id": str(HISTORY),
        "member_id": str(raj),
        "reason": "shared",
        "added": True,
        "shared_by": str((await _member(db_sessionmaker, event.world_id, "helen")).id),
        "question": question,
    }


async def test_sharing_is_limited_to_visible_threads_and_your_own_org(app: FastAPI) -> None:
    async with visitor(app) as v, visitor(app) as other:
        await v.start()
        await other.start(scenario="minimal")
        members = await v.member_ids()
        foreign = (await other.member_ids())["sam"]

        async def share(thread_id: UUID, member_id: UUID, expect: int) -> None:
            await v.request(
                "POST",
                f"/threads/{thread_id}/share",
                json={"member_id": str(member_id)},
                expect=expect,
            )

        await share(HISTORY, members["alex"], 400)  # yourself
        await share(HISTORY, members["helen"], 403)  # another org
        await share(HISTORY, foreign, 404)  # another world
        await share(HISTORY, uuid4(), 404)
        await share(RAJ_THREAD, members["maya"], 404)  # a thread Alex can't see
        await share(uuid4(), members["maya"], 404)
        await v.request(
            "POST",
            f"/threads/{HISTORY}/share",
            json={"member_id": str(members["maya"]), "question": "x" * 2001},
            expect=422,
        )
        assert await _reasons(v) == INBOXES["alex"]

        await v.act_as("raj")
        await share(HISTORY, members["helen"], 404)
        assert await _reasons(v) == INBOXES["raj"]


# --- Cross-world isolation ------------------------------------------------------------


async def _member(sm: Sessionmaker, world_id: UUID, slug: str) -> Member:
    async with sm() as session:
        return (
            await session.scalars(
                select(Member).where(Member.world_id == world_id, Member.slug == slug)
            )
        ).one()


async def test_worlds_with_the_same_seed_ids_stay_apart(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as a, visitor(app) as b:
        world_a = UUID((await a.start())["world_id"])
        world_b = UUID((await b.start())["world_id"])
        assert world_a != world_b
        assert await a.member_ids() == await b.member_ids()
        snapshot_a = await a.json("GET", f"/threads/{HISTORY}")
        helen = await _member(db_sessionmaker, world_a, "helen")

        # Change world B underneath its visitor: a share, a rename, a state change, and
        # a thread that exists only in world A.
        await b.act_as("helen")
        await b.json(
            "POST",
            f"/threads/{HISTORY}/share",
            json={"member_id": str((await b.member_ids())["raj"])},
        )
        async with db_sessionmaker.begin() as session:
            await session.execute(
                update(Member)
                .where(Member.world_id == world_b, Member.slug == "helen")
                .values(name="Helen (world B)")
            )
            await session.execute(
                update(Thread)
                .where(Thread.world_id == world_b, Thread.id == HISTORY)
                .values(state="active")
            )
            for model in (ThreadAccess, ThreadMessage):
                await session.execute(
                    delete(model).where(model.world_id == world_b, model.thread_id == RAJ_THREAD)
                )
            await session.execute(
                delete(Thread).where(Thread.world_id == world_b, Thread.id == RAJ_THREAD)
            )

        assert (await b.json("GET", f"/threads/{HISTORY}"))["state"] == "active"
        assert await a.json("GET", f"/threads/{HISTORY}") == snapshot_a
        await b.act_as("raj")
        assert await _reasons(b) == {HISTORY: ["shared"]}
        await a.act_as("raj")
        assert await _reasons(a) == INBOXES["raj"]

        await a.act_as("helen")
        await b.act_as("helen")
        assert (await a.session())["member"]["name"] == helen.name
        assert (await b.session())["member"]["name"] == "Helen (world B)"
        assert set(await _reasons(a)) == {HISTORY, RAJ_THREAD}
        assert set(await _reasons(b)) == {HISTORY}
        await a.request("GET", f"/threads/{RAJ_THREAD}")
        await b.request("GET", f"/threads/{RAJ_THREAD}", expect=404)

    assert (await _member(db_sessionmaker, world_a, "helen")).name == helen.name == "Dr. Helen Ward"
