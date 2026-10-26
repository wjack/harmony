"""E4's API routes over a seeded desk world: inbox, thread workspace and notes,
escalation actions, org settings, network view, demo story, requests and chats.

The data is written through the domain services as the activities would; routes that
run a workflow Update get a Temporal dev server with no workflow running, which is how
they answer when a thread, request or chat has already ended."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from pydantic import JsonValue, SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment

from harmony.agents import consult as answerer
from harmony.agents import context, transcript
from harmony.agents.consult import ConsultDraft
from harmony.agents.models import EscalationResolution, Route
from harmony.agents.runtime import OwedResult, text_block
from harmony.agents.transcript import RepSideTranscript, WireMessage
from harmony.api.app import create_app
from harmony.config import Settings
from harmony.db import repo
from harmony.db.models import Conversation, Escalation, Event, RepNotice, RepSession, Thread
from harmony.db.uow import unit_of_work
from harmony.domain import escalations, routing, threads
from harmony.domain import settings as settings_domain
from harmony.temporal.activities.agents import rep_opening
from harmony.temporal.activities.network import (
    ApplyResolutionInput,
    NetworkActivities,
    ThreadStateInput,
)
from tests.integration._e2_world import DANA
from tests.integration._e2_world import open_thread as e2_open_thread
from tests.integration._e4_world import (
    ALEX,
    CALDER,
    HELEN,
    HENDERSON,
    MAYA,
    NORTHLAKE,
    RAJ,
    RIDGELINE,
    Outreach,
    approved_outreach,
    open_thread,
    routed_consult,
)
from tests.integration._visitors import ADMIN_SECRET, HISTORY, Visitor, cursor_of, visitor

type Sessionmaker = async_sessionmaker[AsyncSession]
type Json = dict[str, Any]


def _app(
    settings: Settings, engine: AsyncEngine, env: WorkflowEnvironment | None = None
) -> FastAPI:
    return create_app(
        settings.model_copy(update={"admin_secret": SecretStr(ADMIN_SECRET)}),
        engine=engine,
        temporal=env.client if env is not None else None,
    )


@pytest.fixture
def app(app_settings: Settings, db_engine: AsyncEngine) -> FastAPI:
    return _app(app_settings, db_engine)


async def _world(v: Visitor) -> UUID:
    await v.start()
    return UUID((await v.session())["world_id"])


async def _story(v: Visitor, sessionmaker: Sessionmaker) -> tuple[Outreach, UUID, UUID]:
    """Alex's request to Northlake and Calder; Northlake's thread with a consult routed
    to Helen. Returns (outreach, thread, escalation)."""
    world_id = await _world(v)
    outreach = await approved_outreach(sessionmaker, world_id, NORTHLAKE, CALDER)
    thread_id = await open_thread(sessionmaker, outreach, NORTHLAKE)
    escalation_id = await routed_consult(sessionmaker, world_id, thread_id)
    return outreach, thread_id, escalation_id


def _ids(items: list[Json]) -> list[UUID]:
    return [UUID(item["thread_id"]) for item in items]


# --- Inbox ----------------------------------------------------------------------------


async def test_the_inbox_sorts_threads_into_its_sections(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        outreach, thread_id, escalation_id = await _story(v, db_sessionmaker)

        await v.act_as("helen")
        response = await v.request("GET", "/inbox")
        assert cursor_of(response) > 0
        inbox = response.json()
        assert _ids(inbox["needs_you"]) == [thread_id]
        item = inbox["needs_you"][0]
        assert item["side"] == "responder"
        assert item["counterparty"]["slug"] == "henderson"
        # The brief is Henderson's: before its representative has written, the request
        # is only Henderson's.
        assert item["title"] == "Request from Henderson Lab"
        assert item["escalation"]["id"] == str(escalation_id)
        assert item["escalation"]["kind"] == "consult"
        assert item["escalation"]["question"] == "Can you run 14 powders before Nov 6?"
        assert item["escalation"]["reason"] == "It draws on the internal reserve block."
        assert [m["slug"] for m in item["escalation"]["assignees"]] == ["helen"]
        # Henderson's chat is Alex's: nobody at Northlake answers in it.
        assert item["conversation_id"] is None
        assert HISTORY in _ids(inbox["done"])
        assert set(inbox) == {
            "needs_you",
            "with_your_team",
            "claude_handling",
            "shared_with_you",
            "done",
        }

        await v.act_as("alex")
        inbox = await v.json("GET", "/inbox")
        assert _ids(inbox["claude_handling"]) == [thread_id]
        # His own chat sent the request: his side's questions are answered there.
        assert inbox["claude_handling"][0]["conversation_id"] == str(outreach.conversation_id)
        # Northlake's inbox item is Northlake's own; Alex sees whose team is checking.
        assert inbox["claude_handling"][0]["escalation"] is None
        assert inbox["claude_handling"][0]["thread_state"] == "awaiting_principal"
        assert inbox["claude_handling"][0]["side_to_move"] == "responder"
        assert inbox["needs_you"] == []

        await v.act_as("raj")
        assert thread_id not in _ids((await v.json("GET", "/inbox"))["shared_with_you"])

        # Reassigned to Raj, the item waits on a teammate, not on Claude.
        async with db_sessionmaker() as session:
            await session.execute(
                update(Escalation)
                .where(Escalation.id == escalation_id)
                .values(assignees=[str(RAJ)])
            )
            await session.commit()
        await v.act_as("helen")
        inbox = await v.json("GET", "/inbox")
        assert inbox["needs_you"] == []
        assert thread_id not in _ids(inbox["claude_handling"])
        assert _ids(inbox["with_your_team"]) == [thread_id]
        waiting = inbox["with_your_team"][0]["escalation"]
        assert [m["slug"] for m in waiting["assignees"]] == ["raj"]

        # Helen's answer settles her item, not the conversation: it stays with Claude
        # until the thread ends.
        async with db_sessionmaker() as session:
            await session.execute(
                update(Escalation)
                .where(Escalation.id == escalation_id)
                .values(status="resolved", resolved_by_member_id=HELEN)
            )
            await session.commit()
        await v.act_as("helen")
        inbox = await v.json("GET", "/inbox")
        assert _ids(inbox["claude_handling"]) == [thread_id]
        assert inbox["claude_handling"][0]["escalation"]["status"] == "resolved"
        assert thread_id not in _ids(inbox["done"])


async def test_a_responder_names_the_request_by_what_crossed_never_the_brief(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        outreach, thread_id, _ = await _story(v, db_sessionmaker)
        async with unit_of_work(db_sessionmaker, outreach.world_id) as uow:
            await threads.append_message(
                uow,
                thread_id,
                message_id=uuid4(),
                from_org_id=HENDERSON,
                kind="message",
                # Representatives introduce themselves; the title is what follows.
                body=(
                    "Hello, this is Claude for Henderson Lab. Could you run powder XRD on"
                    " 14 air-sensitive samples?\nDetails follow."
                ),
                provenance="agent",
            )
            await uow.commit()
        opening = "Could you run powder XRD on 14 air-sensitive samples?"

        await v.act_as("helen")
        inbox = await v.json("GET", "/inbox")
        listed = await v.json("GET", "/threads")
        network = await v.json("GET", "/network", params={"request": str(outreach.request_id)})
        assert [item["title"] for item in inbox["needs_you"]] == [opening]
        assert {row["title"] for row in listed if row["id"] == str(thread_id)} == {opening}
        titles = {row["id"]: row["title"] for row in network["requests"]}
        assert titles[str(outreach.request_id)] == opening
        for body in (inbox, listed, network):
            assert "$2,000" not in str(body)

        await v.act_as("alex")
        listed = await v.json("GET", "/threads")
        assert {row["title"] for row in listed if row["id"] == str(thread_id)} == {
            "Powder XRD on 14 air-sensitive powders by Nov 6. Up to $2,000; ask Alex before…"
        }


async def test_a_note_mention_lands_in_shared_with_you(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        _, thread_id, _ = await _story(v, db_sessionmaker)
        await v.act_as("helen")
        note = await v.json(
            "POST", f"/threads/{thread_id}/notes", json={"body": "@raj is Nov 4 still free?"}
        )
        assert note["seq"] == 1
        assert note["author"]["slug"] == "helen"
        assert [m["slug"] for m in note["mentions"]] == ["raj"]

        await v.act_as("raj")
        shared = (await v.json("GET", "/inbox"))["shared_with_you"]
        assert _ids(shared) == [thread_id]
        assert shared[0]["shared_by"]["slug"] == "helen"
        assert shared[0]["shared_question"] == "@raj is Nov 4 still free?"
        assert "shared" in shared[0]["access_reasons"]

        # A bystander org can't write in the margin of a thread it isn't part of.
        await v.act_as("dana")
        await v.request("POST", f"/threads/{thread_id}/notes", json={"body": "hi"}, expect=404)


# --- Workspace ------------------------------------------------------------------------


async def test_each_side_sees_only_its_own_workspace(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        _, thread_id, escalation_id = await _story(v, db_sessionmaker)
        await v.act_as("helen")
        await v.json("POST", f"/threads/{thread_id}/notes", json={"body": "Checking the block."})
        response = await v.request("GET", f"/threads/{thread_id}/workspace")
        assert cursor_of(response) > 0
        workspace = response.json()
        assert workspace["side"] == "responder"
        assert [c["status"] for c in workspace["consults"]] == ["needs_human"]
        (escalation,) = workspace["escalations"]
        assert escalation["id"] == str(escalation_id)
        assert escalation["status"] == "open"
        assert escalation["current_draft_rev"] == 1
        assert escalation["reason"] == "It draws on the internal reserve block."
        assert [m["slug"] for m in escalation["assignees"]] == ["helen"]
        (draft,) = escalation["drafts"]
        assert draft["answer"].startswith("Yes: Nov 4")
        assert draft["route"] == {"assignees": ["helen"], "reason": "Helen owns the XRD schedule."}
        assert set(draft) == {"draft_rev", "answer", "route", "rationale", "activity", "created_at"}
        assert [n["body"] for n in workspace["notes"]] == ["Checking the block."]
        # Northlake's representative hasn't taken a turn yet.
        assert (workspace["representative"], workspace["close_summary"]) == (None, None)

        await v.act_as("alex")
        workspace = await v.json("GET", f"/threads/{thread_id}/workspace")
        assert workspace["side"] == "requester"
        assert (workspace["consults"], workspace["escalations"], workspace["notes"]) == (
            [],
            [],
            [],
        )

        # A teammate without access (Raj wasn't routed or shared it) gets nothing.
        await v.act_as("raj")
        await v.request("GET", f"/threads/{thread_id}/workspace", expect=404)
        await v.request("GET", f"/threads/{uuid4()}/workspace", expect=404)


async def test_a_drafts_route_is_served_as_the_members_it_named(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        outreach, thread_id, escalation_id = await _story(v, db_sessionmaker)
        loose = ConsultDraft(
            answer="Nov 4 works.",
            needs_human=True,
            needs_human_reason="A reserve slot.",
            route=Route(assignees=["@Helen", "nobody", " helen "], reason="Helen's schedule."),
            rationale="r",
        )
        async with unit_of_work(db_sessionmaker, outreach.world_id) as uow:
            await escalations.add_draft(uow, escalation_id, loose, turn_id=None, draft_rev=2)
            await uow.commit()

        await v.act_as("helen")
        workspace = await v.json("GET", f"/threads/{thread_id}/workspace")
        (escalation,) = workspace["escalations"]
        # As routing read it, so "Routed to you by Claude: …" still holds.
        assert [m["slug"] for m in escalation["assignees"]] == ["helen"]
        assert escalation["drafts"][-1]["route"] == {
            "assignees": ["helen"],
            "reason": "Helen's schedule.",
        }


async def test_drafts_and_close_summaries_stay_with_their_side_after_the_thread_ends(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        outreach, thread_id, escalation_id = await _story(v, db_sessionmaker)
        # The activity `ThreadWorkflow._end("closed")` runs; it never reaches Temporal.
        activities = NetworkActivities(cast(Client, None), db_sessionmaker)
        await activities.set_thread_state(
            ThreadStateInput(world_id=outreach.world_id, thread_id=thread_id, state="closed")
        )
        async with unit_of_work(db_sessionmaker, outreach.world_id) as uow:
            for side, summary in (
                ("requester", "Booked Nov 4."),
                ("responder", "Used Raj's block."),
            ):
                uow.session.add(
                    RepSession(
                        world_id=outreach.world_id,
                        thread_id=thread_id,
                        side=side,
                        system_text="",
                        tools_text="",
                        model="test",
                        close_summary=summary,
                    )
                )
            await uow.commit()

        await v.act_as("alex")
        workspace = await v.json("GET", f"/threads/{thread_id}/workspace")
        assert (workspace["escalations"], workspace["close_summary"]) == ([], "Booked Nov 4.")
        status = await v.json("GET", f"/requests/{outreach.request_id}")
        northlake = next(row for row in status["recipients"] if row["org"]["slug"] == "northlake")
        assert northlake["close_summary"] == "Booked Nov 4."
        network = await v.json("GET", "/network")
        (edge,) = network["edges"]
        assert edge["close_summary"] == "Booked Nov 4."
        # A member who can't see the thread doesn't get the request at all.
        await v.act_as("raj")
        params = {"request": str(outreach.request_id)}
        await v.request("GET", "/network", params=params, expect=404)
        await v.request("GET", f"/requests/{outreach.request_id}", expect=404)

        await v.act_as("helen")
        workspace = await v.json("GET", f"/threads/{thread_id}/workspace")
        assert workspace["close_summary"] == "Used Raj's block."
        assert [e["id"] for e in workspace["escalations"]] == [str(escalation_id)]
        network = await v.json("GET", "/network", params={"request": str(outreach.request_id)})
        assert [edge["close_summary"] for edge in network["edges"]] == ["Used Raj's block."]


def _frozen(world_id: UUID, thread_id: UUID) -> RepSession:
    return RepSession(
        world_id=world_id,
        thread_id=thread_id,
        side="responder",
        system_text="",
        tools_text="[]",
        model="test",
    )


async def _responder_call(
    sessionmaker: Sessionmaker,
    world_id: UUID,
    thread_id: UUID,
    owed: Sequence[OwedResult] = (),
    *,
    stored: bool = True,
) -> None:
    """One call of Northlake's representative as `rep_turn` makes it: the turn's notices
    flushed, then (unless the call failed first) its opening, the notices and the
    response stored."""
    turn_id = uuid4()
    async with unit_of_work(sessionmaker, world_id) as uow:
        notices = await context.flush_notices(
            uow, thread_id=thread_id, side="responder", turn_id=turn_id
        )
        opening = rep_opening(owed, [], counterparty_name="Henderson Lab")
        sent = [WireMessage.of("user", list[JsonValue](opening))]
        if notices is not None:
            sent.append(WireMessage.of("system", [text_block(notices)]))
        sent.append(WireMessage.of("assistant", [text_block("Noted.")]))
        if stored:
            side = RepSideTranscript(world_id=world_id, thread_id=thread_id, side="responder")
            await transcript.append_in(uow, side, sent, turn_id=turn_id)
        await uow.commit()


async def test_what_the_representative_saw_lists_the_notices_it_was_sent(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    """A mid-thread sharing edit replaces what the frozen prompt says (R§12), so the
    inspector lists the notice once a stored call has carried it."""
    async with visitor(app) as v:
        outreach, thread_id, _ = await _story(v, db_sessionmaker)
        world_id = outreach.world_id
        async with unit_of_work(db_sessionmaker, world_id) as uow:
            uow.session.add(_frozen(world_id, thread_id))
            await uow.commit()
        await v.act_as("helen")
        version = (await v.json("GET", "/orgs/northlake/harmony"))["policy_version"]
        sharing = "Reserve-slot availability, including Raj's Wed Nov 4 block."
        await v.json(
            "PUT",
            "/orgs/northlake/harmony",
            json={"policy_version": version, "sharing_description": sharing},
        )

        # A call that failed before it was stored never gave the notice.
        await _responder_call(db_sessionmaker, world_id, thread_id, stored=False)
        workspace = await v.json("GET", f"/threads/{thread_id}/workspace")
        assert workspace["representative"]["notices"] == []

        await _responder_call(db_sessionmaker, world_id, thread_id)
        workspace = await v.json("GET", f"/threads/{thread_id}/workspace")
        (notice,) = workspace["representative"]["notices"]
        assert notice["kind"] == "sharing"
        assert notice["text"] == settings_domain.sharing_notice(
            [context.quoted("sharing", sharing, for_=context.SHARING_FOR_ANY)],
            side="responder",
            counterparty_name="Henderson Lab",
        )


async def test_what_the_representative_saw_lists_every_result_its_side_gave_it(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    """A declined consult's "what you may say" and a held message's decline note reach
    the representative, so the inspector lists them with the released answers, once
    a stored call has given them: a thread can end first."""
    async with visitor(app) as v:
        outreach, thread_id, escalation_id = await _story(v, db_sessionmaker)
        world_id = outreach.world_id
        async with unit_of_work(db_sessionmaker, world_id) as uow:
            uow.session.add(_frozen(world_id, thread_id))
            held, _ = await escalations.open(
                uow,
                escalation_id=uuid4(),
                org_id=NORTHLAKE,
                kind="outbound_review",
                dedupe_key=f"outbound:{uuid4()}",
                thread_id=thread_id,
                side="responder",
            )
            reason = "A teammate approves every message in Ask mode."
            draft = ConsultDraft(
                answer="Acme holds Nov 4.",
                needs_human=True,
                needs_human_reason=reason,
                route=None,
                rationale=reason,
            )
            await escalations.add_draft(uow, held.id, draft, turn_id=None, draft_rev=1)
            await routing.apply_route(uow, held, None)
            await uow.commit()
        activities = NetworkActivities(cast(Client, None), db_sessionmaker)
        owed: list[OwedResult] = []
        for item, text in (
            (escalation_id, "The reserve block is for internal work."),
            (held.id, "Don't name clients."),
        ):
            applied = await activities.apply_resolution(
                ApplyResolutionInput(
                    world_id=world_id,
                    thread_id=thread_id,
                    resolution=EscalationResolution(
                        escalation_id=item,
                        action="decline",
                        text=text,
                        member_id=HELEN,
                        org_id=NORTHLAKE,
                    ),
                    message_id=uuid4(),
                )
            )
            assert applied.owed is not None
            owed.append(applied.owed)

        await v.act_as("helen")
        workspace = await v.json("GET", f"/threads/{thread_id}/workspace")
        assert workspace["representative"]["released"] == []

        await _responder_call(db_sessionmaker, world_id, thread_id, owed)
        workspace = await v.json("GET", f"/threads/{thread_id}/workspace")
        released = workspace["representative"]["released"]
        assert [(row["question"], row["text"]) for row in released] == [
            (
                "Can you run 14 powders before Nov 6?",
                answerer.result_text("decline", "The reserve block is for internal work."),
            ),
            (
                None,
                answerer.held_result(
                    "decline", text="Don't name clients.", drafted="Acme holds Nov 4."
                ),
            ),
        ]
        assert {row["released_by_member"]["slug"] for row in released} == {"helen"}


# --- Escalations ----------------------------------------------------------------------


@pytest.fixture
def temporal_app(
    app_settings: Settings, db_engine: AsyncEngine, temporal_env: WorkflowEnvironment
) -> FastAPI:
    return _app(app_settings, db_engine, temporal_env)


async def test_escalation_actions_check_who_and_whether_it_is_open(
    temporal_app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(temporal_app) as v:
        outreach, _, escalation_id = await _story(v, db_sessionmaker)
        path = f"/escalations/{escalation_id}"
        approve = {"action": "approve", "text": "Yes, Nov 4."}

        # The requester can't see Northlake's item; Raj isn't routed it.
        for slug in ("alex", "raj"):
            await v.act_as(slug)
            problem = await v.json("POST", f"{path}/resolve", json=approve, expect=404)
            assert problem["code"] == "not_found"

        await v.act_as("helen")
        # Only Northlake's members can take it.
        alex = (await v.member_ids())["alex"]
        problem = await v.json(
            "POST", f"{path}/reassign", json={"member_id": str(alex)}, expect=404
        )
        assert problem["code"] == "not_found"
        # No workflow waits on it (its thread ended): it isn't open any more.
        problem = await v.json("POST", f"{path}/resolve", json=approve, expect=409)
        assert problem["code"] == "escalation_not_open"
        problem = await v.json(
            "POST", f"{path}/redraft", json={"instruction": "Shorter."}, expect=409
        )
        assert problem["code"] == "escalation_not_open"

        async with db_sessionmaker() as session:
            await session.execute(
                update(Escalation)
                .where(Escalation.world_id == outreach.world_id, Escalation.id == escalation_id)
                .values(status="resolved", resolved_by_member_id=HELEN)
            )
            await session.commit()
        problem = await v.json("POST", f"{path}/resolve", json=approve, expect=409)
        assert problem["code"] == "escalation_not_open"
        assert problem["resolved_by"]["slug"] == "helen"


async def test_card_actions_on_ended_workflows_conflict(
    temporal_app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(temporal_app) as v:
        outreach, _, _ = await _story(v, db_sessionmaker)
        await v.act_as("alex")
        problem = await v.json("POST", f"/cards/{outreach.card_id}/approve", json={}, expect=409)
        assert problem["code"] == "card_not_pending"

        await v.act_as("helen")
        await v.request("POST", f"/cards/{outreach.card_id}/approve", json={}, expect=404)


# --- Org settings ---------------------------------------------------------------------


async def _notices(
    sessionmaker: Sessionmaker, world_id: UUID, thread_id: UUID
) -> list[tuple[str, str]]:
    """Northlake's (the responder's) pending notices on the thread, oldest first."""
    async with sessionmaker() as session:
        rows = await session.execute(
            select(RepNotice.kind, RepNotice.text)
            .where(
                RepNotice.world_id == world_id,
                RepNotice.thread_id == thread_id,
                RepNotice.side == "responder",
            )
            .order_by(RepNotice.seq)
        )
        return [(kind, text) for kind, text in rows.all()]


async def test_members_read_and_admins_change_harmony_settings(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        outreach, thread_id, _ = await _story(v, db_sessionmaker)
        alex = (await v.member_ids())["alex"]

        await v.act_as("raj")
        settings = await v.json("GET", "/orgs/northlake/harmony")
        assert settings["editable"] is False
        assert {m["member"]["slug"] for m in settings["members"]} >= {"helen", "raj"}
        assert settings["public_summary"].startswith("Northlake Characterization Core")
        assert "external academic price list" in settings["sharing_description"]
        assert (settings["autonomy"], settings["default_assignee"]["slug"]) == ("auto", "helen")
        (trusted,) = settings["trusted_organizations"]
        assert (trusted["counterparty"]["slug"], trusted["autonomy"], trusted["version"]) == (
            "ridgeline",
            None,
            1,
        )
        assert trusted["sharing_description"].startswith("Our dome-holder dimensions")
        version = settings["policy_version"]
        problem = await v.json(
            "PUT",
            "/orgs/northlake/harmony",
            json={"policy_version": version, "autonomy": "ask"},
            expect=403,
        )
        assert problem["code"] == "not_admin"
        await v.request("GET", "/orgs/henderson/harmony", expect=404)

        await v.act_as("helen")
        before = await v.json("GET", "/orgs/northlake/harmony")
        assert before["editable"] is True
        updated = await v.json(
            "PUT",
            "/orgs/northlake/harmony",
            json={
                "policy_version": version,
                "autonomy": "ask",
                "instructions": "Be brief.",
                "sharing_description": "Our public price list only.",
            },
        )
        assert (updated["autonomy"], updated["instructions"]) == ("ask", "Be brief.")
        assert updated["sharing_description"] == "Our public price list only."
        assert updated["public_summary"] == before["public_summary"]
        assert updated["settings_version"] == before["settings_version"] + 1
        assert updated["policy_version"] == version + 1

        # A stale version conflicts; the default assignee must be Northlake's; a
        # required setting can't be nulled.
        problem = await v.json(
            "PUT",
            "/orgs/northlake/harmony",
            json={"policy_version": version, "autonomy": "auto"},
            expect=409,
        )
        assert problem["code"] == "invalid"
        for change in ({"default_assignee_id": str(alex)}, {"autonomy": None}):
            problem = await v.json(
                "PUT",
                "/orgs/northlake/harmony",
                json={"policy_version": version + 1, **change},
                expect=422,
            )
            assert problem["code"] == "invalid"
        # Clearing the default assignee is allowed (routing falls back to an admin).
        cleared = await v.json(
            "PUT",
            "/orgs/northlake/harmony",
            json={"policy_version": version + 1, "default_assignee_id": None},
        )
        assert cleared["default_assignee"] is None

    # The live thread's Northlake side hears about each change at its next turn.
    notices = await _notices(db_sessionmaker, outreach.world_id, thread_id)
    assert [kind for kind, _ in notices] == ["settings", "mode", "sharing"]
    assert "a teammate now reviews every answer" in notices[1][1]
    assert "Our public price list only." in notices[2][1]


async def test_admins_edit_their_members_profiles(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        outreach, thread_id, _ = await _story(v, db_sessionmaker)
        ids = await v.member_ids()
        profile = "Runs the XRD and SEM queues; owns the reserve block."

        await v.act_as("raj")
        version = (await v.json("GET", "/orgs/northlake/harmony"))["policy_version"]
        body = {"policy_version": version, "member_profiles": {str(ids["raj"]): profile}}
        problem = await v.json("PUT", "/orgs/northlake/harmony", json=body, expect=403)
        assert problem["code"] == "not_admin"

        await v.act_as("helen")
        updated = await v.json("PUT", "/orgs/northlake/harmony", json=body)
        profiles = {m["member"]["slug"]: m["profile"] for m in updated["members"]}
        assert profiles["raj"] == profile
        assert profiles["helen"] != ""  # members left out are unchanged
        # Profiles aren't policy: no version bump, so the same version still saves.
        assert updated["policy_version"] == version

        # Only Northlake's own members, and never a null map or an oversized profile.
        for change in ({str(ids["alex"]): "Not ours."}, {str(uuid4()): "Nobody."}, None):
            problem = await v.json(
                "PUT",
                "/orgs/northlake/harmony",
                json={"policy_version": version, "member_profiles": change},
                expect=422,
            )
            assert problem["code"] == "invalid"
        await v.request(
            "PUT",
            "/orgs/northlake/harmony",
            json={"policy_version": version, "member_profiles": {str(ids["raj"]): "x" * 20_001}},
            expect=422,
        )
        assert (await v.json("GET", "/orgs/northlake/harmony"))["members"] == updated["members"]

    # One snapshot of Raj, on Northlake's org stream; no live thread was told anything.
    async with db_sessionmaker() as session:
        snapshots = list(
            await session.scalars(
                select(Event).where(
                    Event.world_id == outreach.world_id,
                    Event.entity_type == "member",
                    Event.entity_id == ids["raj"],
                )
            )
        )
    [snapshot] = snapshots
    assert (snapshot.scope_kind, snapshot.org_id) == ("org", NORTHLAKE)
    assert snapshot.payload["data"]["profile"] == profile
    assert await _notices(db_sessionmaker, outreach.world_id, thread_id) == []


async def test_trusted_organizations_are_admin_only_and_versioned(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    path = "/orgs/northlake/trusted-organizations"
    async with visitor(app) as v:
        outreach, _, _ = await _story(v, db_sessionmaker)
        body = {"counterparty_org_id": str(HENDERSON), "autonomy": "ask"}

        await v.act_as("raj")
        problem = await v.json("POST", path, json=body, expect=403)
        assert problem["code"] == "not_admin"
        # Another org's admin can't reach Northlake's list at all.
        await v.act_as("dana")
        await v.json("POST", path, json=body, expect=404)

        await v.act_as("helen")
        added = await v.json(
            "POST", path, json={**body, "sharing_description": "Also share: our holder specs."}
        )
        assert (added["counterparty"]["slug"], added["autonomy"], added["version"]) == (
            "henderson",
            "ask",
            1,
        )
        assert added["updated_by"]["slug"] == "helen"
        problem = await v.json("POST", path, json=body, expect=409)
        assert problem["code"] == "invalid"
        # Not itself, and only an organization of this world.
        for org_id in (NORTHLAKE, uuid4()):
            problem = await v.json(
                "POST", path, json={"counterparty_org_id": str(org_id)}, expect=422
            )
            assert problem["code"] == "invalid"

        one = f"{path}/{HENDERSON}"
        changed = await v.json("PUT", one, json={"version": 1, "autonomy": None})
        assert (changed["autonomy"], changed["version"]) == (None, 2)
        assert changed["sharing_description"] == "Also share: our holder specs."
        problem = await v.json("PUT", one, json={"version": 1, "autonomy": "skip"}, expect=409)
        assert problem["code"] == "invalid"
        problem = await v.json(
            "PUT", one, json={"version": 2, "sharing_description": None}, expect=422
        )
        assert problem["code"] == "invalid"
        await v.json("PUT", f"{path}/{CALDER}", json={"version": 1}, expect=404)

        await v.act_as("raj")
        await v.json("PUT", one, json={"version": 2, "autonomy": "skip"}, expect=403)
        await v.json("DELETE", one, params={"version": 2}, expect=403)

        await v.act_as("helen")
        await v.json("DELETE", one, params={"version": 1}, expect=409)
        await v.request("DELETE", one, params={"version": 2}, expect=204)
        await v.json("DELETE", one, params={"version": 2}, expect=404)
        settings = await v.json("GET", "/orgs/northlake/harmony")
        assert [row["counterparty"]["slug"] for row in settings["trusted_organizations"]] == [
            "ridgeline"
        ]

    # Every write reached Northlake's org stream; the removal says so.
    async with db_sessionmaker() as session:
        snapshots = list(
            await session.scalars(
                select(Event)
                .where(
                    Event.world_id == outreach.world_id,
                    Event.entity_type == "trusted_organization",
                )
                .order_by(Event.seq)
            )
        )
    assert [(e.scope_kind, e.org_id, e.entity_version) for e in snapshots] == [
        ("org", NORTHLAKE, 1),
        ("org", NORTHLAKE, 2),
        ("org", NORTHLAKE, 3),
    ]
    assert snapshots[-1].payload["data"]["deleted"] is True


async def test_trusted_organizations_stay_in_their_world(app: FastAPI) -> None:
    path = "/orgs/northlake/trusted-organizations"
    async with visitor(app) as a, visitor(app) as b:
        await _world(a)
        await _world(b)
        await a.act_as("helen")
        await b.act_as("helen")
        await a.json("POST", path, json={"counterparty_org_id": str(HENDERSON)})

        # The same seeded org ids, but world B's Northlake never trusted Henderson.
        settings = await b.json("GET", "/orgs/northlake/harmony")
        assert [row["counterparty"]["slug"] for row in settings["trusted_organizations"]] == [
            "ridgeline"
        ]
        await b.json("PUT", f"{path}/{HENDERSON}", json={"version": 1}, expect=404)
        await b.request("DELETE", f"{path}/{HENDERSON}", params={"version": 1}, expect=404)
        added = await b.json("POST", path, json={"counterparty_org_id": str(HENDERSON)})
        assert added["version"] == 1
        await a.request("DELETE", f"{path}/{HENDERSON}", params={"version": 1}, expect=204)
        settings = await b.json("GET", "/orgs/northlake/harmony")
        assert {row["counterparty"]["slug"] for row in settings["trusted_organizations"]} == {
            "henderson",
            "ridgeline",
        }


async def test_a_trusted_organizations_terms_reach_only_its_threads(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    path = f"/orgs/northlake/trusted-organizations/{RIDGELINE}"
    async with visitor(app) as v:
        outreach, henderson_thread, _ = await _story(v, db_sessionmaker)
        world_id = outreach.world_id
        ridgeline = await e2_open_thread(
            db_sessionmaker, world_id, requester=RIDGELINE, sender=DANA, responder=NORTHLAKE
        )
        ridgeline_thread = ridgeline.thread_id

        await v.act_as("helen")
        await v.json("PUT", path, json={"version": 1, "autonomy": "skip"})
        await v.json(
            "PUT", path, json={"version": 2, "sharing_description": "Also: our holder drawings."}
        )
        notices = await _notices(db_sessionmaker, world_id, ridgeline_thread)
        assert [kind for kind, _ in notices] == ["mode", "sharing"]
        assert notices[0][1] == settings_domain.mode_notice("skip")
        sharing = notices[1][1]
        assert "external academic price list" in sharing
        assert 'for="Ridgeline' in sharing
        assert "Also: our holder drawings." in sharing

        # Back to the org's defaults when Northlake stops trusting Ridgeline.
        await v.request("DELETE", path, params={"version": 3}, expect=204)
        notices = await _notices(db_sessionmaker, world_id, ridgeline_thread)
        assert [kind for kind, _ in notices] == ["mode", "sharing", "mode", "sharing"]
        assert notices[2][1] == settings_domain.mode_notice("auto")
        assert "holder drawings" not in notices[3][1]

        # Henderson's thread with Northlake was never told anything.
        assert await _notices(db_sessionmaker, world_id, henderson_thread) == []


# --- Network, story, requests, chats ---------------------------------------------------


async def test_the_network_view_focuses_the_latest_request(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        outreach, thread_id, _ = await _story(v, db_sessionmaker)

        await v.act_as("alex")
        response = await v.request("GET", "/network")
        assert cursor_of(response) > 0
        network = response.json()
        assert network["request_id"] == str(outreach.request_id)
        nodes = {node["org"]["slug"]: node for node in network["nodes"]}
        assert nodes["henderson"]["role"] == "requester"
        assert (nodes["northlake"]["role"], nodes["northlake"]["participant"]) == (
            "recipient",
            "needs_human",
        )
        # Who Northlake waits on is Northlake's to know.
        assert nodes["northlake"]["waiting_on"] is None
        assert (nodes["calder"]["participant"], nodes["calder"]["fit"]) == ("screened", "strong")
        assert nodes["ridgeline"]["role"] == "bystander"
        assert {slug for slug, node in nodes.items() if node["suggested"]} == {
            "northlake",
            "ridgeline",
            "calder",
        }
        (edge,) = network["edges"]
        assert edge["thread_id"] == str(thread_id)
        assert edge["turn_count"] == 0
        assert network["counts"] == {
            "orgs": len(nodes),
            "threads": 1,
            "active": 1,
            "closed": 0,
        }

        await v.act_as("helen")
        network = await v.json("GET", "/network", params={"request": str(outreach.request_id)})
        nodes = {node["org"]["slug"]: node for node in network["nodes"]}
        assert nodes["northlake"]["waiting_on"]["member"]["slug"] == "helen"
        await v.request("GET", "/network", params={"request": str(uuid4())}, expect=404)


async def test_the_network_view_opens_on_no_finished_seeded_request(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        world_id = await _world(v)
        await v.act_as("alex")
        network = await v.json("GET", "/network")
        # Alex's seeded September request is listed to pick, but nothing opens on it.
        assert network["requests"]
        assert all(request["status"] == "closed" for request in network["requests"])
        assert network["request_id"] is None
        assert network["edges"] == []

        # Picked, it shows how it went, without the story's suggestions.
        seeded = network["requests"][0]["id"]
        network = await v.json("GET", "/network", params={"request": seeded})
        assert network["request_id"] == seeded
        assert not any(node["suggested"] for node in network["nodes"])

        # A request sent from Alex's chat opens by default, with them.
        outreach = await approved_outreach(db_sessionmaker, world_id, NORTHLAKE)
        network = await v.json("GET", "/network")
        assert network["request_id"] == str(outreach.request_id)
        assert any(node["suggested"] for node in network["nodes"])


async def test_a_recipient_sees_only_its_own_part_of_the_request(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        outreach, thread_id, _ = await _story(v, db_sessionmaker)
        await open_thread(db_sessionmaker, outreach, CALDER)

        await v.act_as("helen")
        network = await v.json("GET", "/network")
        assert network["request_id"] == str(outreach.request_id)
        shown = {
            node["org"]["slug"]: (node["role"], node["fit"], node["participant"], node["suggested"])
            for node in network["nodes"]
            if node["role"] != "bystander" or node["suggested"]
        }
        # Not that Henderson also asked Calder, how it rated anyone, or the story's picks.
        assert shown == {
            "henderson": ("requester", None, None, False),
            "northlake": ("recipient", None, "needs_human", False),
        }
        assert [edge["thread_id"] for edge in network["edges"]] == [str(thread_id)]
        assert network["counts"]["threads"] == 1

        # The requester's members (and the lens) still see every recipient.
        await v.act_as("alex")
        network = await v.json("GET", "/network")
        assert len(network["edges"]) == 2


async def test_a_request_shows_what_a_thread_says_only_to_those_who_can_open_it(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        world_id = await _world(v)
        # Maya's request; Alex is routed into Northlake's thread only.
        outreach = await approved_outreach(db_sessionmaker, world_id, NORTHLAKE, CALDER, owner=MAYA)
        northlake = await open_thread(db_sessionmaker, outreach, NORTHLAKE)
        calder = await open_thread(db_sessionmaker, outreach, CALDER)
        said = ((northlake, NORTHLAKE, "Nov 4 works."), (calder, CALDER, "Client X pays $325."))
        async with unit_of_work(db_sessionmaker, world_id) as uow:
            for thread_id, org_id, line in said:
                await threads.append_message(
                    uow,
                    thread_id,
                    message_id=uuid4(),
                    from_org_id=org_id,
                    kind="message",
                    body=line,
                    provenance="agent",
                )
            thread = await repo.get_required(uow.session, Thread, world_id, northlake)
            await threads.add_access(uow, thread, member_id=ALEX, org_id=HENDERSON)
            # Maya holds a Henderson item on Calder's thread.
            held, _ = await escalations.open(
                uow,
                escalation_id=uuid4(),
                org_id=HENDERSON,
                kind="outbound_review",
                dedupe_key=f"outbound:{uuid4()}",
                thread_id=calder,
                side="requester",
            )
            await escalations.assign(uow, held.id, member_ids=[MAYA])
            await uow.commit()

        def lines(status: Json) -> dict[str, str | None]:
            return {row["org"]["slug"]: row["latest_line"] for row in status["recipients"]}

        def waits(network: Json) -> dict[str, Any]:
            return {node["org"]["slug"]: node["waiting_on"] for node in network["nodes"]}

        await v.act_as("alex")
        await v.request("GET", f"/threads/{calder}", expect=404)
        seen = await v.json("GET", f"/requests/{outreach.request_id}")
        northlake_said = "Northlake Characterization Core: Nov 4 works."
        assert lines(seen) == {"northlake": northlake_said, "calder": None}
        # Maya's chat is hers alone: nothing points Alex at a chat he can't open.
        assert seen["conversation_id"] is None
        (hidden,) = (row for row in seen["recipients"] if row["org"]["slug"] == "calder")
        assert (hidden["thread_id"], hidden["close_summary"], hidden["waiting_on"]) == (
            str(calder),
            None,
            None,
        )
        # The network view tells him no more about Calder's thread than the request does.
        network = await v.json("GET", "/network", params={"request": str(outreach.request_id)})
        assert waits(network)["calder"] is None
        assert {node["org"]["slug"]: node["participant"] for node in network["nodes"]}[
            "calder"
        ] == "needs_human"

        await v.act_as("maya")
        network = await v.json("GET", "/network", params={"request": str(outreach.request_id)})
        assert waits(network)["calder"]["member"]["slug"] == "maya"

        await v.act_as("maya")
        owned = await v.json("GET", f"/requests/{outreach.request_id}")
        assert lines(owned) == {
            "northlake": northlake_said,
            "calder": "Calder Computational: Client X pays $325.",
        }
        assert owned["conversation_id"] == str(outreach.conversation_id)


async def test_the_story_suggests_the_message_to_its_sender(app: FastAPI) -> None:
    async with visitor(app) as v:
        await _world(v)
        await v.act_as("alex")
        story = await v.json("GET", "/demo/story")
        assert story["scenario"] == "harmony-demo"
        assert story["acting_as"]["slug"] == "alex"
        assert story["suggested_message"].startswith("Find a lab")
        assert "{" not in story["suggested_message"]
        assert [org["slug"] for org in story["suggested_recipients"]] == [
            "northlake",
            "ridgeline",
            "calder",
        ]
        assert story["steps"][0]["act_as"]["slug"] == "alex"

        await v.act_as("helen")
        story = await v.json("GET", "/demo/story")
        assert story["suggested_message"] is None
        # The picks are Henderson's: Northlake's members aren't told to ask themselves.
        assert story["suggested_recipients"] == []

        await v.act_as("maya")
        story = await v.json("GET", "/demo/story")
        assert story["suggested_message"] is None
        assert [org["slug"] for org in story["suggested_recipients"]] == [
            "northlake",
            "ridgeline",
            "calder",
        ]


class Unreachable:
    """A Temporal client whose every workflow start fails, as when the server is down."""

    async def start_workflow(self, *_: Any, **__: Any) -> None:
        raise RPCError("connection refused", RPCStatusCode.UNAVAILABLE, b"")


async def test_a_chat_whose_workflow_never_starts_leaves_no_empty_chat(
    app_settings: Settings, db_engine: AsyncEngine, db_sessionmaker: Sessionmaker
) -> None:
    app = create_app(
        app_settings.model_copy(update={"admin_secret": SecretStr(ADMIN_SECRET)}),
        engine=db_engine,
        temporal=cast(Client, Unreachable()),
    )
    async with visitor(app) as v:
        world_id = await _world(v)
        await v.act_as("alex")
        with pytest.raises(RPCError):
            await v.request("POST", "/conversations", json={"text": "Find a lab for XRD."})
        assert await v.json("GET", "/conversations") == []
    async with db_sessionmaker() as session:
        left = await session.scalars(select(Conversation).where(Conversation.world_id == world_id))
        assert list(left) == []
        # The owner's Recents hear it went.
        events = await session.scalars(
            select(Event)
            .where(Event.world_id == world_id, Event.entity_type == "conversation")
            .order_by(Event.seq)
        )
        last = list(events)[-1]
        assert (last.entity_version, last.payload["data"]) == (
            2,
            {"id": str(last.entity_id), "deleted": True},
        )


async def test_requests_and_chats_are_their_owners(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        outreach, thread_id, _ = await _story(v, db_sessionmaker)

        await v.act_as("alex")
        status = await v.json("GET", f"/requests/{outreach.request_id}")
        recipients = {row["org"]["slug"]: row for row in status["recipients"]}
        assert recipients["northlake"]["thread_id"] == str(thread_id)
        chats = await v.json("GET", "/conversations")
        assert [chat["id"] for chat in chats] == [str(outreach.conversation_id)]
        chat = await v.json("GET", f"/conversations/{outreach.conversation_id}")
        card, request = chat["items"]
        assert (card["type"], card["status"]) == ("outreach_card", "approved")
        assert (request["type"], request["request_id"]) == (
            "request_status",
            str(outreach.request_id),
        )
        assert chat["pending_card_id"] is None

        await v.act_as("helen")
        await v.request("GET", f"/requests/{outreach.request_id}", expect=404)
        await v.request("GET", f"/conversations/{outreach.conversation_id}", expect=404)
        assert await v.json("GET", "/conversations") == []
