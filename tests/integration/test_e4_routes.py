"""E4's API routes over a seeded desk world: inbox, thread workspace and notes,
escalation actions, org settings, network view, demo story, requests and chats.

The data is written through the domain services as the activities would; routes that
run a workflow Update get a Temporal dev server with no workflow running, which is how
they answer when a thread, request or chat has already ended."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from pydantic import JsonValue, SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from harmony.agents import consult as answerer
from harmony.agents import context, transcript
from harmony.agents.consult import ConsultDraft
from harmony.agents.models import (
    CardResolution,
    EscalationResolution,
    OutreachCardPayload,
    OutreachEdits,
    OutreachRecipient,
    Route,
    RouteAssignee,
)
from harmony.agents.runtime import OwedResult, text_block
from harmony.agents.transcript import RepSideTranscript, WireMessage
from harmony.api.app import create_app
from harmony.api.routes import cards as cards_route
from harmony.api.routes.problems import from_update_failure
from harmony.auth.session import current_actor, resolve_actor
from harmony.config import Settings
from harmony.db import repo
from harmony.db.models import (
    Conversation,
    Escalation,
    Event,
    NetworkRequest,
    RepNotice,
    RepSession,
    Steer,
    Thread,
    World,
)
from harmony.db.uow import unit_of_work
from harmony.domain import cards, escalations, requests, routing, steers, threads
from harmony.domain import settings as settings_domain
from harmony.domain.errors import CardNotPending
from harmony.temporal.activities.agents import (
    GuidanceWritten,
    RepTurnInput,
    RepTurnResult,
    SteerPartInput,
    rep_opening,
)
from harmony.temporal.activities.network import (
    ApplyResolutionInput,
    NetworkActivities,
    ThreadStateInput,
)
from harmony.temporal.policies import MAIN_TASK_QUEUE, MODEL_TASK_QUEUE
from harmony.temporal.workflows.payloads import (
    STEER_RUNNING_ERROR,
    CardResolved,
    ThreadInput,
    thread_workflow_id,
)
from harmony.temporal.workflows.thread import ThreadWorkflow
from tests.integration._e2_world import DANA
from tests.integration._e2_world import open_thread as e2_open_thread
from tests.integration._e4_world import (
    ALEX,
    BRIEF,
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
    propose,
    routed_consult,
    start_chat,
)
from tests.integration._visitors import ADMIN_SECRET, HISTORY, Visitor, cursor_of, visitor
from tests.integration._worlds import demote_to_member

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
        helen = (await v.act_as("helen"))["member"]["id"]
        note = await v.json(
            "POST",
            f"/threads/{thread_id}/notes",
            json={"body": "@raj is Nov 4 still free?", "as_member_id": helen},
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
        dana = (await v.act_as("dana"))["member"]["id"]
        await v.request(
            "POST",
            f"/threads/{thread_id}/notes",
            json={"body": "hi", "as_member_id": dana},
            expect=404,
        )


async def test_a_note_from_a_tab_showing_another_member_is_refused(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    """A tab still showing Helen after a switch to Raj in another tab: 409
    `member_changed` naming Raj, checked before the thread is read, and nothing stored."""
    async with visitor(app) as v:
        _, thread_id, _ = await _story(v, db_sessionmaker)
        helen = (await v.act_as("helen"))["member"]["id"]
        raj = (await v.act_as("raj"))["member"]

        for thread in (thread_id, uuid4()):
            refused = await v.json(
                "POST",
                f"/threads/{thread}/notes",
                json={"body": "Checking the block.", "as_member_id": helen},
                expect=409,
            )
            assert refused["code"] == "member_changed"
            assert refused["member"] == {k: raj[k] for k in ("id", "slug", "name")}

        await v.act_as("helen")
        assert (await v.json("GET", f"/threads/{thread_id}/workspace"))["notes"] == []


# --- Workspace ------------------------------------------------------------------------


async def test_each_side_sees_only_its_own_workspace(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        _, thread_id, escalation_id = await _story(v, db_sessionmaker)
        helen = (await v.act_as("helen"))["member"]["id"]
        await v.json(
            "POST",
            f"/threads/{thread_id}/notes",
            json={"body": "Checking the block.", "as_member_id": helen},
        )
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
        assert draft["route"] == {
            "assignees": [{"slug": "helen", "why": "Helen owns the XRD schedule."}]
        }
        assert draft["short_question"] is None
        assert set(draft) == {
            "draft_rev",
            "answer",
            "short_question",
            "route",
            "rationale",
            "activity",
            "rewritten_by",
            "created_at",
        }
        assert [n["body"] for n in workspace["notes"]] == ["Checking the block."]
        # Northlake's representative hasn't taken a turn yet, nor been steered.
        assert (workspace["representative"], workspace["close_summary"]) == (None, None)
        assert (workspace["steers"], workspace["guidance"]) == ([], None)

        await v.act_as("alex")
        workspace = await v.json("GET", f"/threads/{thread_id}/workspace")
        assert workspace["side"] == "requester"
        assert (workspace["consults"], workspace["escalations"], workspace["notes"]) == (
            [],
            [],
            [],
        )

        # A teammate without access (Raj, a member, wasn't routed or shared it) gets nothing.
        await demote_to_member(db_sessionmaker, await _world(v), "raj")
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
            route=Route(
                assignees=[
                    RouteAssignee(member="@Helen", why="Helen's schedule."),
                    RouteAssignee(member="nobody", why="Nobody's."),
                    RouteAssignee(member=" helen ", why="Said twice."),
                ]
            ),
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
        # Each named member once, with the first why given for them.
        assert escalation["drafts"][-1]["route"] == {
            "assignees": [{"slug": "helen", "why": "Helen's schedule."}]
        }


async def test_drafts_and_close_summaries_stay_with_their_side_after_the_thread_ends(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        outreach, thread_id, escalation_id = await _story(v, db_sessionmaker)
        # The activity `ThreadWorkflow._end("closed")` runs; it never reaches Temporal.
        activities = NetworkActivities(cast(Client, None), db_sessionmaker)
        await activities.set_thread_state(
            ThreadStateInput(
                world_id=outreach.world_id,
                thread_id=thread_id,
                state="closed",
                end_reason="closed",
                closed_by="requester",
            )
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
        await demote_to_member(db_sessionmaker, outreach.world_id, "raj")
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
                    "decline",
                    text="Don't name clients.",
                    drafted="Acme holds Nov 4.",
                    resolved="Acme holds Nov 4.",
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

        # The requester can't see Northlake's item; Raj, a member, isn't routed it.
        await demote_to_member(db_sessionmaker, outreach.world_id, "raj")
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
        # A rewrite is an Edit now (POST /threads/{id}/steers), not an action on the item.
        await v.request("POST", f"{path}/redraft", json={"instruction": "Shorter."}, expect=405)
        revise = {"action": "revise", "text": "Shorter."}
        await v.request("POST", f"{path}/resolve", json=revise, expect=422)

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


async def test_a_steer_is_its_sides_own_and_needs_a_running_thread(
    temporal_app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(temporal_app) as v:
        outreach, thread_id, escalation_id = await _story(v, db_sessionmaker)
        path = f"/threads/{thread_id}/steers"
        raj = (await v.member_ids())["raj"]
        helen = (await v.act_as("helen"))["member"]["id"]
        edit = {"client_key": str(uuid4()), "as_member_id": helen, "body": "Shorter."}

        problem = await v.json("POST", path, json={**edit, "as_member_id": str(raj)}, expect=409)
        assert problem["code"] == "member_changed"
        problem = await v.json("POST", path, json={**edit, "body": "  "}, expect=422)
        assert problem["code"] == "invalid"
        problem = await v.json(
            "POST", path, json={**edit, "escalation_id": str(uuid4())}, expect=404
        )
        assert problem["code"] == "not_found"
        # No workflow runs this seeded thread, so nothing can take the steer.
        on_item = {**edit, "escalation_id": str(escalation_id), "draft_text": "Yes: Nov 4."}
        problem = await v.json("POST", path, json=on_item, expect=409)
        assert problem["code"] == "thread_closed"

        # Northlake's item isn't the requester's to steer.
        alex = (await v.act_as("alex"))["member"]["id"]
        problem = await v.json("POST", path, json={**on_item, "as_member_id": alex}, expect=404)
        assert problem["code"] == "not_found"

        # A steer and the guidance it wrote are their side's alone.
        async with unit_of_work(db_sessionmaker, outreach.world_id) as uow:
            steer = await steers.record(
                uow,
                steer_id=uuid4(),
                thread_id=thread_id,
                org_id=NORTHLAKE,
                kind="edit",
                author_member_id=HELEN,
                body="Keep the reserve out of it.",
            )
            await steers.add_guidance(uow, steer, "Don't mention the reserve block.")
            await uow.commit()
        workspace = await v.json("GET", f"/threads/{thread_id}/workspace")
        assert (workspace["steers"], workspace["guidance"]) == ([], None)
        await v.act_as("helen")
        workspace = await v.json("GET", f"/threads/{thread_id}/workspace")
        (shown,) = workspace["steers"]
        assert (shown["body"], shown["author"]["slug"]) == ("Keep the reserve out of it.", "helen")
        guidance = workspace["guidance"]
        assert (guidance["version"], guidance["text"]) == (1, "Don't mention the reserve block.")
        assert guidance["delivered_at"] is None


@activity.defn(name="rep_turn")
async def _endless_turn(turn: RepTurnInput) -> RepTurnResult:
    """The requester's first turn, still running when the worker stops."""
    await asyncio.Event().wait()
    raise AssertionError("never set")


@activity.defn(name="write_guidance")
async def _no_guidance(part: SteerPartInput) -> GuidanceWritten:
    """Nothing written, so each steer settles as soon as it starts."""
    return GuidanceWritten(outcome="unchanged")


@asynccontextmanager
async def _live_thread(
    env: WorkflowEnvironment, sessionmaker: Sessionmaker, outreach: Outreach, thread_id: UUID
) -> AsyncIterator[None]:
    """The thread's `ThreadWorkflow`, running its Updates with the real database
    activities. Its requester's turn never ends, so the thread stays live."""
    network = NetworkActivities(env.client, sessionmaker)
    main = Worker(
        env.client,
        task_queue=MAIN_TASK_QUEUE,
        workflows=[ThreadWorkflow],
        activities=[network.record_steer, network.settle_steer, network.set_thread_state],
    )
    model = Worker(
        env.client, task_queue=MODEL_TASK_QUEUE, activities=[_endless_turn, _no_guidance]
    )
    async with main, model:
        handle = await env.client.start_workflow(
            ThreadWorkflow.run,
            ThreadInput(
                world_id=outreach.world_id,
                thread_id=thread_id,
                request_id=outreach.request_id,
                requester_org_id=HENDERSON,
                responder_org_id=NORTHLAKE,
                cassette_thread="henderson.northlake",
            ),
            id=thread_workflow_id(outreach.world_id, thread_id),
            task_queue=MAIN_TASK_QUEUE,
        )
        try:
            yield
        finally:
            await handle.terminate()


async def _steers_of(sessionmaker: Sessionmaker, world_id: UUID) -> list[tuple[UUID, str, str]]:
    async with sessionmaker() as session:
        rows = await session.execute(
            select(Steer.author_member_id, Steer.body, Steer.status)
            .where(Steer.world_id == world_id)
            .order_by(Steer.seq)
        )
        return [(author, body, status) for author, body, status in rows.all()]


async def test_a_steer_starts_for_whoever_can_see_the_thread_and_never_reuses_a_key(
    temporal_app: FastAPI, temporal_env: WorkflowEnvironment, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(temporal_app) as v:
        outreach, thread_id, _ = await _story(v, db_sessionmaker)
        world_id = outreach.world_id
        path = f"/threads/{thread_id}/steers"
        # Helen, routed the consult, can see the thread as a member; Raj can't.
        for slug in ("helen", "raj"):
            await demote_to_member(db_sessionmaker, world_id, slug)
        ids = await v.member_ids()
        key = uuid4()
        edit = {"client_key": str(key), "as_member_id": str(ids["helen"]), "body": "Shorter."}
        async with _live_thread(temporal_env, db_sessionmaker, outreach, thread_id):
            await v.act_as("helen")
            started = await v.json("POST", path, json=edit, expect=202)
            steer_id = steers.steer_id_for(world_id, NORTHLAKE, HELEN, key)
            assert started == {"steer_id": str(steer_id), "escalation_id": None, "draft_rev": None}
            # A retried POST is the same steer; the key with other text is refused, not
            # answered with the first steer's 202.
            assert await v.json("POST", path, json=edit, expect=202) == started
            other = {**edit, "body": "Longer."}
            problem = await v.json("POST", path, json=other, expect=422)
            assert problem["code"] == "invalid"

            # Neither Raj (Northlake, no access) nor Priya (Calder, not a party) is told
            # the thread exists, and neither sends an Update.
            for slug in ("raj", "priya"):
                await v.act_as(slug)
                outsider = {**edit, "client_key": str(uuid4()), "as_member_id": str(ids[slug])}
                problem = await v.json("POST", path, json=outsider, expect=404)
                assert problem["code"] == "not_found"
        assert await _steers_of(db_sessionmaker, world_id) == [(HELEN, "Shorter.", "failed")]


def test_a_steer_id_is_one_members_one_key_in_one_world() -> None:
    world, key = uuid4(), uuid4()
    steer_id = steers.steer_id_for(world, NORTHLAKE, HELEN, key)
    assert steers.steer_id_for(world, NORTHLAKE, HELEN, key) == steer_id
    others = [
        steers.steer_id_for(uuid4(), NORTHLAKE, HELEN, key),
        steers.steer_id_for(world, HENDERSON, HELEN, key),
        steers.steer_id_for(world, NORTHLAKE, RAJ, key),
        steers.steer_id_for(world, NORTHLAKE, HELEN, uuid4()),
    ]
    assert steer_id not in others


async def test_a_steer_refused_while_another_runs_names_who_sent_that_one(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        world_id = await _world(v)
    # As `ThreadWorkflow.validate_steer` rejects it.
    busy = ApplicationError(
        "Claude is still working on an earlier edit",
        {"by_member_id": str(HELEN)},
        type=STEER_RUNNING_ERROR,
    )
    error = await from_update_failure(busy, db_sessionmaker, world_id)
    assert (error.status_code, error.problem.code) == (409, "steer_running")
    assert error.problem.by is not None
    assert error.problem.by.slug == "helen"


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
        await demote_to_member(db_sessionmaker, outreach.world_id, "raj")

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
        await demote_to_member(db_sessionmaker, outreach.world_id, "raj")

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
        await demote_to_member(db_sessionmaker, outreach.world_id, "raj")

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
        await demote_to_member(db_sessionmaker, world_id, "alex")
        # Maya's request; Alex, a member, is routed into Northlake's thread only.
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


# --- Send mode, auto cards, asks and titles (LIVE_DEMO_PLAN 3.9–3.14) ----------------

TITLE: Final = "Powder XRD on 14 powders, up to $2,000"
NORTHLAKE_TITLE: Final = "Air-free powder XRD on 14 samples"
NORTHLAKE_ASK: Final = "Can you run 14 air-free powders by Nov 6?"


def _suggesting(*, auto_send: list[UUID] | None = None) -> OutreachCardPayload:
    """Northlake and Ridgeline suggested; Calder listed for the member to decide."""
    return OutreachCardPayload(
        brief=BRIEF,
        title=TITLE,
        recipients=[
            OutreachRecipient(
                org_id=NORTHLAKE,
                fit="strong",
                reason="Air-free XRD.",
                ask=NORTHLAKE_ASK,
                title=NORTHLAKE_TITLE,
                suggested=True,
            ),
            OutreachRecipient(
                org_id=RIDGELINE, fit="strong", reason="Glovebox XRD.", suggested=True
            ),
            OutreachRecipient(org_id=CALDER, fit="possible", reason="SEM only.", suggested=False),
        ],
        auto_send_recipients=auto_send,
    )


async def _sent_to(sm: Sessionmaker, world_id: UUID, request_id: UUID) -> list[Json]:
    async with sm() as session:
        request = await repo.get_required(session, NetworkRequest, world_id, request_id)
        return cast(list[Json], request.recipients)


def _selected(card: Json) -> dict[str, bool]:
    return {row["org"]["slug"]: row["selected"] for row in card["recipients"]}


async def test_an_approval_without_edits_sends_the_suggested_rows(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        world_id = await _world(v)
        outreach = await approved_outreach(db_sessionmaker, world_id, payload=_suggesting())
        sent = await _sent_to(db_sessionmaker, world_id, outreach.request_id)
        assert [row["org_id"] for row in sent] == [str(NORTHLAKE), str(RIDGELINE)]
        assert (sent[0]["ask"], sent[0]["title"], sent[0]["suggested"]) == (
            NORTHLAKE_ASK,
            NORTHLAKE_TITLE,
            True,
        )
        # A card from before suggestions goes to the rows whose fit isn't "no".
        legacy = OutreachCardPayload(
            brief=BRIEF,
            recipients=[
                OutreachRecipient(org_id=NORTHLAKE, fit="strong", reason="Air-free XRD."),
                OutreachRecipient(org_id=CALDER, fit="no", reason="SEM only."),
            ],
        )
        old = await approved_outreach(db_sessionmaker, world_id, payload=legacy)
        old_sent = await _sent_to(db_sessionmaker, world_id, old.request_id)
        assert [row["org_id"] for row in old_sent] == [str(NORTHLAKE)]

        await v.act_as("alex")
        chat = await v.json("GET", f"/conversations/{outreach.conversation_id}")
        card, _ = chat["items"]
        assert (card["status"], card["title"], card["auto"]) == ("approved", TITLE, False)
        assert _selected(card) == {"northlake": True, "ridgeline": True, "calder": False}
        rows = {row["org"]["slug"]: row for row in card["recipients"]}
        assert (rows["northlake"]["ask"], rows["northlake"]["title"]) == (
            NORTHLAKE_ASK,
            NORTHLAKE_TITLE,
        )
        assert (rows["calder"]["ask"], rows["calder"]["suggested"]) == (None, False)

        status = await v.json("GET", f"/requests/{outreach.request_id}")
        assert status["title"] == TITLE
        # Calder was only proposed: its row says so, apart from a recipient still waiting.
        assert {row["org"]["slug"]: row["sent"] for row in status["recipients"]} == {
            "northlake": True,
            "ridgeline": True,
            "calder": False,
        }


async def test_edited_asks_and_recipients_reach_the_request(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    calder_ask = "Could you image the powders by SEM after the XRD?"
    async with visitor(app) as v:
        world_id = await _world(v)
        edits = OutreachEdits(recipients=[NORTHLAKE, CALDER], asks={CALDER: calder_ask})
        outreach = await approved_outreach(
            db_sessionmaker, world_id, payload=_suggesting(), edits=edits
        )
        sent = await _sent_to(db_sessionmaker, world_id, outreach.request_id)
        assert [(row["org_id"], row["ask"]) for row in sent] == [
            (str(NORTHLAKE), NORTHLAKE_ASK),
            (str(CALDER), calder_ask),
        ]

        await v.act_as("alex")
        chat = await v.json("GET", f"/conversations/{outreach.conversation_id}")
        card, _ = chat["items"]
        assert _selected(card) == {"northlake": True, "ridgeline": False, "calder": True}
        assert {row["org"]["slug"]: row["ask"] for row in card["recipients"]}["calder"] == (
            calder_ask
        )

        # The API takes asks only for proposed recipients.
        conversation_id = await start_chat(db_sessionmaker, world_id)
        card_id = await propose(db_sessionmaker, world_id, conversation_id, payload=_suggesting())
        problem = await v.json(
            "POST",
            f"/cards/{card_id}/approve",
            json={"asks": {str(HENDERSON): "Anything?"}},
            expect=422,
        )
        assert problem["code"] == "invalid"


async def test_a_card_sent_without_review_is_never_the_members_to_settle(
    temporal_app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(temporal_app) as v:
        world_id = await _world(v)
        auto_chat = await start_chat(db_sessionmaker, world_id)
        auto_card = await propose(
            db_sessionmaker,
            world_id,
            auto_chat,
            payload=_suggesting(auto_send=[NORTHLAKE]),
            auto=True,
        )
        review_chat = await start_chat(db_sessionmaker, world_id)
        review_card = await propose(db_sessionmaker, world_id, review_chat, payload=_suggesting())

        await v.act_as("alex")
        for action, body in (("approve", {}), ("reject", {"feedback": "Not yet."})):
            problem = await v.json("POST", f"/cards/{auto_card}/{action}", json=body, expect=409)
            assert problem["code"] == "card_not_pending"

        chat = await v.json("GET", f"/conversations/{auto_chat}")
        assert chat["pending_card_id"] is None
        (card,) = chat["items"]
        assert (card["status"], card["auto"]) == ("pending", True)
        assert _selected(card) == {"northlake": True, "ridgeline": False, "calder": False}
        # A card awaiting the member lists every row as in the request until they choose.
        chat = await v.json("GET", f"/conversations/{review_chat}")
        assert chat["pending_card_id"] == str(review_card)
        (card,) = chat["items"]
        assert (card["status"], card["auto"]) == ("pending", False)
        assert _selected(card) == {"northlake": True, "ridgeline": True, "calder": True}
        recents = {
            row["id"]: row["has_pending_card"] for row in await v.json("GET", "/conversations")
        }
        assert recents == {str(auto_chat): False, str(review_chat): True}

        # A message is the chat's next one; it rejects nothing.
        posted = await v.json(
            "POST", f"/conversations/{auto_chat}/messages", json={"text": "Ask about SEM too."}
        )
        assert posted["outcome"] == "queued"

        # Only the workflow settles it, approving as the chat's owner.
        member = CardResolution(card_id=auto_card, decision="approve", member_id=ALEX)
        with pytest.raises(CardNotPending):
            async with unit_of_work(db_sessionmaker, world_id) as uow:
                await cards.resolve(uow, member)
        workflow = member.model_copy(update={"auto": True})
        request_id = uuid4()
        async with unit_of_work(db_sessionmaker, world_id) as uow:
            card_row = await cards.resolve(uow, workflow)
            await requests.create_from_card(uow, card_row, workflow, request_id=request_id)
            await uow.commit()
        sent = await _sent_to(db_sessionmaker, world_id, request_id)
        assert [row["org_id"] for row in sent] == [str(NORTHLAKE)]
        chat = await v.json("GET", f"/conversations/{auto_chat}")
        card, _ = chat["items"]
        assert (card["status"], card["auto"]) == ("approved", True)
        assert _selected(card) == {"northlake": True, "ridgeline": False, "calder": False}


async def test_an_outreach_decline_takes_an_optional_reason(
    temporal_app: FastAPI, db_sessionmaker: Sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 10: Decline needs no reason, and the resolution says it was a Decline
    (a message typed while the card waits rejects it too, as `message`)."""
    sent: list[CardResolution] = []

    async def run_update(
        handle: object, name: str, resolution: CardResolution, result_type: type[CardResolved]
    ) -> CardResolved:
        # The conversation's `resolve_card` Update, which stores the resolution.
        sent.append(resolution)
        return CardResolved(card_id=resolution.card_id, status="rejected")

    monkeypatch.setattr(cards_route, "run_update", run_update)
    async with visitor(temporal_app) as v:
        world_id = await _world(v)
        chat_id = await start_chat(db_sessionmaker, world_id)
        card_id = await propose(db_sessionmaker, world_id, chat_id, payload=_suggesting())
        await v.act_as("alex")

        await v.request(
            "POST", f"/cards/{card_id}/reject", json={"feedback": "x" * 4001}, expect=422
        )
        assert sent == []
        resolved = await v.json("POST", f"/cards/{card_id}/reject", json={})

    assert resolved["status"] == "rejected"
    (resolution,) = sent
    assert (resolution.decision, resolution.feedback, resolution.rejected_with) == (
        "reject",
        None,
        "decline",
    )


async def test_an_auto_card_the_workflow_rejects_stays_auto_and_shows_its_proposal(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        world_id = await _world(v)
        chat_id = await start_chat(db_sessionmaker, world_id)
        payload = _suggesting(auto_send=[NORTHLAKE])
        card_id = await propose(db_sessionmaker, world_id, chat_id, payload=payload, auto=True)
        # As the workflow does when the send fails: rejected, with nothing sent.
        rejected = CardResolution(card_id=card_id, decision="reject", member_id=ALEX, auto=True)
        async with unit_of_work(db_sessionmaker, world_id) as uow:
            await cards.resolve(uow, rejected)
            await uow.commit()

        await v.act_as("alex")
        (card,) = (await v.json("GET", f"/conversations/{chat_id}"))["items"]
        assert (card["status"], card["auto"]) == ("rejected", True)
        assert _selected(card) == {"northlake": True, "ridgeline": True, "calder": True}


async def _conversation(sm: Sessionmaker, world_id: UUID, conversation_id: UUID) -> Conversation:
    async with sm() as session:
        return await repo.get_required(session, Conversation, world_id, conversation_id)


async def test_a_chats_owner_overrides_its_send_mode(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        world_id = await _world(v)
        conversation_id = await start_chat(db_sessionmaker, world_id)
        path = f"/conversations/{conversation_id}"
        before = await _conversation(db_sessionmaker, world_id, conversation_id)

        await v.act_as("alex")
        chat = await v.json("GET", path)
        assert chat["send_mode"] == {"override": None, "org_default": False, "effective": False}
        for override, effective in ((True, True), (False, False), (None, False)):
            mode = await v.json("PATCH", path, json={"send_without_confirmation": override})
            assert mode == {"override": override, "org_default": False, "effective": effective}
        # The key is required: null, never a missing key, means the org's default.
        await v.request("PATCH", path, json={}, expect=422)
        after = await _conversation(db_sessionmaker, world_id, conversation_id)
        assert after.version == before.version + 3
        # Not something that happened in the chat: Recents keep their order.
        assert after.last_activity_at == before.last_activity_at

        await v.act_as("maya")
        await v.request("PATCH", path, json={"send_without_confirmation": True}, expect=404)
        version = (await v.json("GET", "/orgs/henderson/harmony"))["policy_version"]
        await v.json(
            "PUT",
            "/orgs/henderson/harmony",
            json={"policy_version": version, "send_without_confirmation": True},
        )

        # With no override, the chat follows its org's default, read live.
        await v.act_as("alex")
        chat = await v.json("GET", path)
        assert chat["send_mode"] == {"override": None, "org_default": True, "effective": True}
        mode = await v.json("PATCH", path, json={"send_without_confirmation": False})
        assert mode == {"override": False, "org_default": True, "effective": False}


async def test_a_send_mode_change_in_a_retired_world_conflicts(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        world_id = await _world(v)
        conversation_id = await start_chat(db_sessionmaker, world_id)
        await v.act_as("alex")
        session_id = UUID((await v.session())["session_id"])
        actor = await resolve_actor(db_sessionmaker, session_id, "desk")
        assert actor is not None
        # Retired after the request resolved its actor, as when a Reset lands mid-request.
        async with db_sessionmaker() as session:
            await session.execute(
                update(World).where(World.id == world_id).values(status="retired")
            )
            await session.commit()
        app.dependency_overrides[current_actor] = lambda: actor
        problem = await v.json(
            "PATCH",
            f"/conversations/{conversation_id}",
            json={"send_without_confirmation": True},
            expect=409,
        )
        assert problem["code"] == "world_retired"


async def test_a_new_chat_stores_its_send_mode(
    temporal_app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(temporal_app) as v:
        world_id = await _world(v)
        await v.act_as("alex")
        created = await v.json(
            "POST",
            "/conversations",
            json={"text": "Find a lab for XRD.", "send_without_confirmation": True},
        )
    conversation = await _conversation(db_sessionmaker, world_id, UUID(created["id"]))
    assert conversation.send_without_confirmation is True


async def test_admins_set_whether_chats_send_without_confirmation(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        outreach, thread_id, _ = await _story(v, db_sessionmaker)
        await demote_to_member(db_sessionmaker, outreach.world_id, "raj")

        await v.act_as("raj")
        settings = await v.json("GET", "/orgs/northlake/harmony")
        assert settings["send_without_confirmation"] is False
        version = settings["policy_version"]
        change = {"policy_version": version, "send_without_confirmation": True}
        problem = await v.json("PUT", "/orgs/northlake/harmony", json=change, expect=403)
        assert problem["code"] == "not_admin"

        await v.act_as("helen")
        updated = await v.json("PUT", "/orgs/northlake/harmony", json=change)
        assert updated["send_without_confirmation"] is True
        assert updated["policy_version"] == version + 1

    # How chats send is nothing a representative is told about.
    assert await _notices(db_sessionmaker, outreach.world_id, thread_id) == []


async def test_a_responder_never_sees_the_requests_own_title(
    app: FastAPI, db_sessionmaker: Sessionmaker
) -> None:
    async with visitor(app) as v:
        world_id = await _world(v)
        payload = OutreachCardPayload(
            brief=BRIEF,
            title=TITLE,
            recipients=[
                OutreachRecipient(org_id=NORTHLAKE, fit="strong", reason="Air-free XRD."),
                OutreachRecipient(
                    org_id=RIDGELINE, fit="strong", reason="Glovebox.", title=NORTHLAKE_TITLE
                ),
            ],
        )
        outreach = await approved_outreach(db_sessionmaker, world_id, payload=payload)
        untitled = await open_thread(db_sessionmaker, outreach, NORTHLAKE)
        titled = await open_thread(db_sessionmaker, outreach, RIDGELINE)
        await routed_consult(db_sessionmaker, world_id, untitled)

        await v.act_as("helen")
        inbox = await v.json("GET", "/inbox")
        bodies = [
            inbox,
            await v.json("GET", "/threads"),
            await v.json("GET", "/network", params={"request": str(outreach.request_id)}),
            await v.json("GET", f"/threads/{untitled}/workspace"),
        ]
        for body in bodies:
            assert "$2,000" not in str(body)
        assert [item["title"] for item in inbox["needs_you"]] == ["Request from Henderson Lab"]

        await v.act_as("alex")
        listed = {row["id"]: row["title"] for row in await v.json("GET", "/threads")}
        # The requester's own name for the request, unless the thread has its own.
        assert (listed[str(untitled)], listed[str(titled)]) == (TITLE, NORTHLAKE_TITLE)

        await v.act_as("dana")
        listed = {row["id"]: row["title"] for row in await v.json("GET", "/threads")}
        # Ridgeline's members see the subject written for them, which both sides share.
        assert listed[str(titled)] == NORTHLAKE_TITLE


def test_a_threads_title_wins_and_the_fallbacks_stay() -> None:
    request = NetworkRequest(
        requester_org_id=HENDERSON, title=None, brief="Powder XRD by Nov 6.\nUp to $2,000."
    )
    thread = Thread(title=None)
    name = "Henderson Lab"

    def title(org_id: UUID, opening: str | None = None) -> str:
        return threads.thread_title(thread, request, org_id, requester=name, opening=opening)

    assert title(HENDERSON) == "Powder XRD by Nov 6."
    assert title(NORTHLAKE) == "Request from Henderson Lab"
    assert title(NORTHLAKE, "Could you run XRD?") == "Could you run XRD?"
    request.title = TITLE
    assert (title(HENDERSON), title(NORTHLAKE)) == (TITLE, "Request from Henderson Lab")
    thread.title = NORTHLAKE_TITLE
    assert (title(HENDERSON), title(NORTHLAKE, "Hi")) == (NORTHLAKE_TITLE, NORTHLAKE_TITLE)
