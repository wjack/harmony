"""Consults, escalations and routing against Postgres (REVISION_UNSTRUCTURED.md §4, §6),
in a seeded harmony-demo world."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from harmony.agents.consult import ConsultDraft, SharingCheck
from harmony.agents.models import EscalationResolution, Route, RouteAssignee
from harmony.db.models import Consult, Escalation, Event, RepPolicy, Thread, ThreadAccess
from harmony.db.models.choices import EscalationKind
from harmony.db.uow import UnitOfWork, unit_of_work
from harmony.domain import consults, escalations, routing
from harmony.domain.errors import ConsultNotOpen, EscalationNotOpen, NotInOrg
from tests.integration._e2_world import (
    ALEX,
    HELEN,
    HENDERSON,
    NORTHLAKE,
    RAJ,
    Opened,
    Sessionmaker,
    open_thread,
    seeded_world,
)

QUESTION = "Can you run XRD on 14 air-sensitive powders by Nov 6?"


async def open_consult(uow: UnitOfWork, opened: Opened, question: str = QUESTION) -> Consult:
    consult, _ = await consults.open(
        uow,
        consult_id=uuid4(),
        thread_id=opened.thread_id,
        side="responder",
        org_id=NORTHLAKE,
        question=question,
        turn_id=uuid4(),
        tool_use_id="toolu_1",
    )
    return consult


async def open_escalation(
    sm: Sessionmaker,
    opened: Opened,
    *,
    kind: EscalationKind = "consult",
    route: Route | None = None,
) -> tuple[UUID, list[UUID]]:
    async with unit_of_work(sm, opened.world_id) as uow:
        consult = await open_consult(uow, opened) if kind == "consult" else None
        escalation, created = await escalations.open(
            uow,
            escalation_id=uuid4(),
            org_id=NORTHLAKE,
            kind=kind,
            dedupe_key=f"test:{uuid4()}",
            thread_id=opened.thread_id,
            side="responder",
            consult_id=consult.id if consult else None,
        )
        assert created
        assignees = await routing.apply_route(uow, escalation, route)
        await uow.commit()
    return escalation.id, assignees


async def routed(sm: Sessionmaker, world_id: UUID, thread_id: UUID) -> set[UUID]:
    async with sm() as session:
        rows = await session.scalars(
            select(ThreadAccess.member_id).where(
                ThreadAccess.world_id == world_id,
                ThreadAccess.thread_id == thread_id,
                ThreadAccess.reason == "routed",
            )
        )
        return set(rows)


def draft(answer: str = "Nov 4 09:00-13:00 works.", *, route: Route | None = None) -> ConsultDraft:
    return ConsultDraft(
        answer=answer,
        needs_human=True,
        needs_human_reason="It draws on the reserve block.",
        route=route,
        rationale="The public calendar starts Nov 10.",
    )


# --- Consults ---------------------------------------------------------------------------


async def test_the_same_question_reuses_the_open_consult(db_sessionmaker: Sessionmaker) -> None:
    sm = db_sessionmaker
    world = await seeded_world(sm)
    opened = await open_thread(sm, world)
    async with unit_of_work(sm, world) as uow:
        first = await open_consult(uow, opened)
        again, created = await consults.open(
            uow,
            consult_id=uuid4(),
            thread_id=opened.thread_id,
            side="responder",
            org_id=NORTHLAKE,
            question="  can you run XRD on 14 air sensitive powders by Nov 6  ",
            turn_id=uuid4(),
            tool_use_id="toolu_2",
        )
        await uow.commit()
    assert (again.id, created) == (first.id, False)
    async with sm() as session:
        thread = await session.get(Thread, (world, opened.thread_id))
        assert thread is not None
        assert (thread.state, thread.side_to_move) == ("awaiting_principal", "responder")
        assert await consults.ordinal(session, first) == 1

    async with unit_of_work(sm, world) as uow:
        released = await consults.release(
            uow, first.id, text="Nov 4 morning works.", released_by="auto"
        )
        await uow.commit()
    assert (released.status, released.released_text, released.released_by) == (
        "released",
        "Nov 4 morning works.",
        "auto",
    )
    assert released.released_by_member_id is None


async def test_the_decision_draft_and_sharing_check_are_written_once(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    world = await seeded_world(sm)
    opened = await open_thread(sm, world)
    route = Route(assignees=[RouteAssignee(member="helen", why="Reserve slots")])
    async with unit_of_work(sm, world) as uow:
        consult = await open_consult(uow, opened)
        turn_id = uuid4()
        await consults.record_draft(uow, consult.id, draft(route=route), answer_turn_id=turn_id)
        await consults.record_draft(uow, consult.id, draft("A redraft"), answer_turn_id=uuid4())
        check = SharingCheck(within_sharing=False, reason="Reserve slots are internal.")
        await consults.record_sharing_check(uow, consult.id, check)
        await consults.record_sharing_check(
            uow, consult.id, SharingCheck(within_sharing=True, reason="later")
        )
        await uow.commit()
    async with sm() as session:
        stored = await session.get(Consult, (world, consult.id))
        assert stored is not None
        assert (stored.answer, stored.answer_turn_id, stored.needs_human) == (
            "Nov 4 09:00-13:00 works.",
            turn_id,
            True,
        )
        assert stored.route == route.model_dump(mode="json")
        assert stored.sharing_check == check.model_dump(mode="json")


async def test_a_release_names_its_releaser_and_a_declined_consult_stays_declined(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    world = await seeded_world(sm)
    opened = await open_thread(sm, world)
    async with unit_of_work(sm, world) as uow:
        consult = await open_consult(uow, opened)
        with pytest.raises(ValueError, match="member"):
            await consults.release(uow, consult.id, text="x", released_by="member")
        released = await consults.release(
            uow, consult.id, text="Kalrez seals.", released_by="member", member_id=HELEN
        )
        assert (released.released_by, released.released_by_member_id) == ("member", HELEN)
        await uow.commit()

    other = await open_thread(sm, world)
    async with unit_of_work(sm, world) as uow:
        declined = await consults.decline(
            uow, (await open_consult(uow, other)).id, text="not ours", member_id=HELEN
        )
        assert (declined.status, declined.released_text) == ("declined", "not ours")
        assert (declined.released_by, declined.released_by_member_id) == ("member", HELEN)
        with pytest.raises(ConsultNotOpen):
            await consults.release(uow, declined.id, text="late", released_by="auto")


# --- Escalations ------------------------------------------------------------------------


async def test_a_dedupe_key_reuses_only_an_open_escalation(db_sessionmaker: Sessionmaker) -> None:
    sm = db_sessionmaker
    world = await seeded_world(sm)
    opened = await open_thread(sm, world)
    key = f"outbound:{uuid4()}"

    async def review(escalation_id: UUID) -> tuple[Escalation, bool]:
        async with unit_of_work(sm, world) as uow:
            opened_escalation = await escalations.open(
                uow,
                escalation_id=escalation_id,
                org_id=HENDERSON,
                kind="outbound_review",
                dedupe_key=key,
                thread_id=opened.thread_id,
                side="requester",
            )
            await uow.commit()
        return opened_escalation

    first, created = await review(uuid4())
    assert created
    same, created = await review(uuid4())
    assert (same.id, created) == (first.id, False)
    async with unit_of_work(sm, world) as uow:
        await escalations.cancel(uow, first.id, reason="thread closed")
        await uow.commit()

    repeat, created = await review(uuid4())
    assert created
    assert repeat.id != first.id
    assert (repeat.status, repeat.dedupe_key) == ("open", f"{key}#{repeat.id}")
    again, created = await review(uuid4())
    assert (again.id, created) == (repeat.id, False)


async def test_resolution_is_first_writer_wins(db_sessionmaker: Sessionmaker) -> None:
    sm = db_sessionmaker
    world = await seeded_world(sm)
    opened = await open_thread(sm, world)
    escalation_id, _ = await open_escalation(
        sm, opened, route=Route(assignees=[RouteAssignee(member="helen", why="x")])
    )

    def resolution(member_id: UUID) -> EscalationResolution:
        return EscalationResolution(
            escalation_id=escalation_id,
            action="approve",
            text="Nov 4 works.",
            member_id=member_id,
            org_id=NORTHLAKE,
        )

    async with unit_of_work(sm, world) as uow:
        resolved = await escalations.resolve(uow, resolution(HELEN))
        await uow.commit()
    assert (resolved.status, resolved.resolved_by_member_id) == ("resolved", HELEN)

    with pytest.raises(EscalationNotOpen) as lost:
        async with unit_of_work(sm, world) as uow:
            await escalations.resolve(uow, resolution(RAJ))
    assert lost.value.resolved_by_member_id == HELEN
    assert lost.value.resolved_at is not None


async def test_only_a_member_of_the_escalations_org_resolves_or_moves_it(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    world = await seeded_world(sm)
    opened = await open_thread(sm, world)
    escalation_id, _ = await open_escalation(sm, opened)

    def approve(member_id: UUID, org_id: UUID) -> EscalationResolution:
        return EscalationResolution(
            escalation_id=escalation_id, action="approve", member_id=member_id, org_id=org_id
        )

    # Alex is Henderson's; Helen claiming Henderson's org is refused too.
    for refused in (approve(ALEX, HENDERSON), approve(ALEX, NORTHLAKE), approve(HELEN, HENDERSON)):
        with pytest.raises(NotInOrg):
            async with unit_of_work(sm, world) as uow:
                await escalations.resolve(uow, refused)
    with pytest.raises(NotInOrg):
        async with unit_of_work(sm, world) as uow:
            await escalations.reassign(uow, escalation_id, to_member_ids=[RAJ], member_id=ALEX)
    async with unit_of_work(sm, world) as uow:
        assert (await escalations.resolve(uow, approve(RAJ, NORTHLAKE))).status == "resolved"
        await uow.commit()


async def test_add_draft_appends_revs(db_sessionmaker: Sessionmaker) -> None:
    sm = db_sessionmaker
    world = await seeded_world(sm)
    opened = await open_thread(sm, world)
    escalation_id, _ = await open_escalation(sm, opened)
    route = Route(assignees=[RouteAssignee(member="helen", why="Reserve slots")])

    async with unit_of_work(sm, world) as uow:
        first = await escalations.add_draft(uow, escalation_id, draft("v1"), turn_id=None)
        second = await escalations.add_draft(
            uow, escalation_id, draft("v2", route=route), turn_id=None
        )
        repeat = await escalations.add_draft(
            uow, escalation_id, draft("ignored"), turn_id=None, draft_rev=2
        )
        await uow.commit()
    assert (first.draft_rev, second.draft_rev, repeat.answer) == (1, 2, "v2")
    async with sm() as session:
        escalation = await session.get(Escalation, (world, escalation_id))
        assert escalation is not None
        assert escalation.current_draft_rev == 2
        current = await escalations.current_draft(session, escalation)
        assert current is not None
        assert (current.answer, current.route) == ("v2", route.model_dump(mode="json"))
        assert current.rationale == "The public calendar starts Nov 10."


async def test_a_redraft_that_wrote_nothing_is_told_to_whoever_sees_the_item(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    world = await seeded_world(sm)
    opened = await open_thread(sm, world)
    escalation_id, _ = await open_escalation(sm, opened)

    async with unit_of_work(sm, world) as uow:
        await escalations.redraft_failed(uow, escalation_id, 2)
        await uow.commit()
    async with sm() as session:
        (event,) = await session.scalars(
            select(Event).where(Event.world_id == world, Event.type == "escalation.redraft_failed")
        )
    # The escalation's own scope: Northlake's members who can see the thread.
    assert (event.scope_kind, event.org_id, event.thread_id) == (
        "thread",
        NORTHLAKE,
        opened.thread_id,
    )
    assert event.payload == {
        "type": "escalation.redraft_failed",
        "escalation_id": str(escalation_id),
        "draft_rev": 2,
    }


# --- Routing ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route",
    [
        None,
        Route(
            assignees=[
                RouteAssignee(member="nobody", why="Guessing"),
                RouteAssignee(member="maya", why="Guessing"),
            ]
        ),
    ],
    ids=["none", "invalid"],
)
async def test_a_missing_or_invalid_route_goes_to_the_default_assignee(
    db_sessionmaker: Sessionmaker, route: Route | None
) -> None:
    sm = db_sessionmaker
    world = await seeded_world(sm)
    opened = await open_thread(sm, world)
    _, assignees = await open_escalation(sm, opened, route=route)
    assert assignees == [HELEN]


async def test_without_a_default_assignee_the_first_admin_gets_it(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    world = await seeded_world(sm)
    async with unit_of_work(sm, world) as uow:
        policy = await uow.session.get(RepPolicy, (world, NORTHLAKE))
        assert policy is not None
        policy.default_assignee_id = None
        await uow.commit()
    opened = await open_thread(sm, world)
    _, assignees = await open_escalation(sm, opened, route=None)
    assert assignees == [HELEN]


async def test_slugs_are_matched_loosely_and_an_assigned_escalation_keeps_its_assignees(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    world = await seeded_world(sm)
    opened = await open_thread(sm, world)
    escalation_id, assignees = await open_escalation(
        sm,
        opened,
        route=Route(
            assignees=[
                RouteAssignee(member="@RAJ", why="Instrument question"),
                RouteAssignee(member=" raj ", why="Instrument question"),
            ]
        ),
    )
    assert assignees == [RAJ]
    async with sm() as session:
        cues = list(
            await session.scalars(
                select(Event.payload).where(
                    Event.world_id == world, Event.type == "demo.needs_human"
                )
            )
        )
    assert [(cue["member_slug"], cue["entity_id"]) for cue in cues] == [("raj", str(escalation_id))]

    async with unit_of_work(sm, world) as uow:
        escalation = await uow.session.get(Escalation, (world, escalation_id))
        assert escalation is not None
        retry = await routing.apply_route(
            uow, escalation, Route(assignees=[RouteAssignee(member="helen", why="r")])
        )
        await uow.commit()
    assert retry == [RAJ]
    assert await routed(sm, world, opened.thread_id) - {ALEX} == {RAJ}


async def test_reassigning_an_item_away_keeps_access_an_earlier_item_gave(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    world = await seeded_world(sm)
    opened = await open_thread(sm, world)
    raj = Route(assignees=[RouteAssignee(member="raj", why="Sample prep")])
    earlier, _ = await open_escalation(sm, opened, route=raj)
    async with unit_of_work(sm, world) as uow:
        await escalations.resolve(
            uow,
            EscalationResolution(
                escalation_id=earlier,
                action="decline",
                member_id=RAJ,
                org_id=NORTHLAKE,
                text="Not this week.",
            ),
        )
        await uow.commit()
    later, _ = await open_escalation(sm, opened, route=raj)

    async with unit_of_work(sm, world) as uow:
        await escalations.reassign(uow, later, to_member_ids=[HELEN], member_id=RAJ)
        await uow.commit()

    # He still follows the conversation his earlier answer is part of.
    assert {RAJ, HELEN} <= await routed(sm, world, opened.thread_id)


async def test_reassign_moves_the_item_and_its_access(db_sessionmaker: Sessionmaker) -> None:
    sm = db_sessionmaker
    world = await seeded_world(sm)
    opened = await open_thread(sm, world)
    escalation_id, _ = await open_escalation(
        sm, opened, route=Route(assignees=[RouteAssignee(member="raj", why="Instrument question")])
    )
    assert RAJ in await routed(sm, world, opened.thread_id)

    for _ in range(2):  # a retry changes nothing
        async with unit_of_work(sm, world) as uow:
            moved = await escalations.reassign(
                uow, escalation_id, to_member_ids=[HELEN], member_id=RAJ
            )
            await uow.commit()
        assert escalations.assignee_ids(moved) == [HELEN]
        assert moved.status == "open"
        access = await routed(sm, world, opened.thread_id)
        assert HELEN in access
        assert RAJ not in access
