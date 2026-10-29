"""The network workflows' database activities (`NetworkActivities`) against Postgres,
called directly as the worker would: card and escalation resolutions with their
effects (a consult released to its one thread, declined or taken over; a held message
or close approved), thread endings, and the workflow starts. Also the mode toward a
counterparty that `rep_turn` and `answer_consult` read live."""

from __future__ import annotations

from typing import Literal, cast
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment

from harmony.agents import consult as answerer
from harmony.agents.consult import ConsultDraft
from harmony.agents.keys import thread_key
from harmony.agents.models import (
    CardResolution,
    EscalationAction,
    EscalationResolution,
    InlineQuestionItem,
    Route,
    RouteAssignee,
    brief_title,
)
from harmony.db import repo
from harmony.db.models import (
    Consult,
    Conversation,
    Escalation,
    Member,
    NetworkRequest,
    RepNotice,
    RepPolicy,
    Steer,
    Thread,
    ThreadMessage,
    TrustedOrganization,
)
from harmony.db.models.choices import Autonomy, EndReason
from harmony.db.uow import unit_of_work
from harmony.domain import consults, conversations, escalations, notes, routing, steers, threads
from harmony.domain.settings import mode_toward
from harmony.temporal.activities.network import (
    REQUEST_LIFETIME,
    ApplyResolutionInput,
    CardResolutionInput,
    NetworkActivities,
    NotifyConversationInput,
    OpenThreadInput,
    ReassignInput,
    RecordSteerInput,
    SettledCardInput,
    SettleSteerInput,
    ThreadStateInput,
    UnsummarizedInput,
)
from harmony.temporal.workflows.payloads import (
    CARD_NOT_PENDING_ERROR,
    ESCALATION_NOT_OPEN_ERROR,
    NOT_IN_ORG_ERROR,
    STEER_CONFLICT_ERROR,
    NetworkUpdate,
    Reassignment,
    SteerRequest,
    conversation_workflow_id,
    thread_workflow_id,
)
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
    approved_outreach,
    consult_draft,
    open_thread,
    outreach_payload,
    propose,
    routed_consult,
    seeded_world,
    start_chat,
)

type Sessionmaker = async_sessionmaker[AsyncSession]


def _offline(sessionmaker: Sessionmaker) -> NetworkActivities:
    # These activities only touch the database; none of them reaches Temporal.
    return NetworkActivities(cast(Client, None), sessionmaker)


def _resolve(
    world_id: UUID,
    thread_id: UUID,
    escalation_id: UUID,
    action: EscalationAction,
    *,
    member_id: UUID = HELEN,
    org_id: UUID = NORTHLAKE,
    **values: object,
) -> ApplyResolutionInput:
    return ApplyResolutionInput(
        world_id=world_id,
        thread_id=thread_id,
        resolution=EscalationResolution.model_validate(
            {
                "escalation_id": escalation_id,
                "action": action,
                "member_id": member_id,
                "org_id": org_id,
                **values,
            }
        ),
        message_id=uuid4(),
    )


async def _thread_with_northlake(sessionmaker: Sessionmaker) -> tuple[UUID, UUID]:
    world_id = await seeded_world(sessionmaker)
    outreach = await approved_outreach(sessionmaker, world_id, NORTHLAKE)
    return world_id, await open_thread(sessionmaker, outreach, NORTHLAKE)


async def _messages(
    sessionmaker: Sessionmaker, world_id: UUID, thread_id: UUID
) -> list[ThreadMessage]:
    async with sessionmaker() as session:
        rows = await session.scalars(
            repo.in_world(ThreadMessage, world_id)
            .where(ThreadMessage.thread_id == thread_id)
            .order_by(ThreadMessage.seq)
        )
        return list(rows)


async def _consult_of(sessionmaker: Sessionmaker, world_id: UUID, thread_id: UUID) -> Consult:
    async with sessionmaker() as session:
        consult = await session.scalar(
            repo.in_world(Consult, world_id).where(Consult.thread_id == thread_id)
        )
    assert consult is not None
    return consult


async def _notices(
    sessionmaker: Sessionmaker, world_id: UUID, thread_id: UUID
) -> list[tuple[str, str, str]]:
    async with sessionmaker() as session:
        rows = await session.scalars(
            repo.in_world(RepNotice, world_id)
            .where(RepNotice.thread_id == thread_id)
            .order_by(RepNotice.side, RepNotice.seq)
        )
        return [(row.side, row.kind, row.text) for row in rows]


# --- Cards ---------------------------------------------------------------------------


async def test_approving_an_outreach_card_creates_its_request(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await seeded_world(db_sessionmaker)
    conversation_id = await start_chat(db_sessionmaker, world_id)
    card_id = await propose(db_sessionmaker, world_id, conversation_id, NORTHLAKE, CALDER)
    activities = _offline(db_sessionmaker)
    request_id = uuid4()
    arg = CardResolutionInput(
        world_id=world_id,
        conversation_id=conversation_id,
        resolution=CardResolution(card_id=card_id, decision="approve", member_id=ALEX),
        request_id=request_id,
    )
    settled = SettledCardInput(world_id=world_id, conversation_id=conversation_id, card_id=card_id)
    assert await activities.settled_card(settled) is None
    applied = await activities.apply_card_resolution(arg)
    # A retry after the commit, or a read-back, reports the same thing.
    assert await activities.apply_card_resolution(arg) == applied
    assert await activities.settled_card(settled) == applied
    assert applied.resolved.status == "approved"
    assert applied.resolved.request_id == request_id
    assert applied.tool_use_id.startswith("toolu_")
    # That it's out, under its title, and nothing to check (decision 7).
    assert applied.content == (
        f'Approved and sent: the Harmony Network Request "{brief_title(BRIEF)}" went to '
        "Northlake Characterization Core and Calder Computational. Its ID, for "
        f"get_request_status only (never show IDs to Alex Chen): {request_id}.\n\n"
        "Their progress shows on the request's card in this chat, and anything that needs "
        "Alex Chen will come up here as a question; you'll be told here when there's news "
        "and when every conversation has ended. There's nothing to check now: in a "
        "sentence, tell Alex Chen it's out and stop."
    )
    async with db_sessionmaker() as session:
        request = await repo.get_required(session, NetworkRequest, world_id, request_id)
    assert request.card_id == card_id

    rejection = arg.model_copy(
        update={
            "resolution": CardResolution(
                card_id=card_id, decision="reject", member_id=ALEX, feedback="No."
            )
        }
    )
    with pytest.raises(ApplicationError) as failed:
        await activities.apply_card_resolution(rejection)
    assert failed.value.type == CARD_NOT_PENDING_ERROR
    assert failed.value.non_retryable


@pytest.mark.parametrize(
    ("feedback", "rejected_with", "content"),
    [
        (
            None,
            "decline",
            "Declined: Alex Chen decided not to send this request. Nothing went out. Don't "
            "propose it again unless Alex Chen asks.",
        ),
        (
            "Too early, we haven't fixed the sample count",
            "decline",
            "Declined: Alex Chen decided not to send this request, and said: Too early, we "
            "haven't fixed the sample count. Nothing went out. Don't propose it again unless "
            "Alex Chen asks.",
        ),
        # A message typed while the card waited replaced it.
        ("Ask Calder too.", "message", "rejected: Ask Calder too."),
    ],
)
async def test_a_rejected_card_says_how_it_was_rejected(
    db_sessionmaker: Sessionmaker,
    feedback: str | None,
    rejected_with: Literal["decline", "message"],
    content: str,
) -> None:
    world_id = await seeded_world(db_sessionmaker)
    conversation_id = await start_chat(db_sessionmaker, world_id)
    card_id = await propose(db_sessionmaker, world_id, conversation_id, NORTHLAKE)
    applied = await _offline(db_sessionmaker).apply_card_resolution(
        CardResolutionInput(
            world_id=world_id,
            conversation_id=conversation_id,
            resolution=CardResolution(
                card_id=card_id,
                decision="reject",
                member_id=ALEX,
                feedback=feedback,
                rejected_with=rejected_with,
            ),
            request_id=uuid4(),
        )
    )
    assert (applied.resolved.status, applied.content) == ("rejected", content)


async def test_a_card_sent_without_review_says_so_and_no_member_can_settle_it(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await seeded_world(db_sessionmaker)
    conversation_id = await start_chat(db_sessionmaker, world_id)
    payload = outreach_payload(NORTHLAKE, CALDER).model_copy(
        update={"auto_send_recipients": [NORTHLAKE]}
    )
    card_id = await propose(db_sessionmaker, world_id, conversation_id, payload=payload, auto=True)
    activities = _offline(db_sessionmaker)

    def resolving(*, auto: bool) -> CardResolutionInput:
        return CardResolutionInput(
            world_id=world_id,
            conversation_id=conversation_id,
            resolution=CardResolution(
                card_id=card_id, decision="approve", member_id=ALEX, auto=auto
            ),
            request_id=request_id,
        )

    request_id = uuid4()
    with pytest.raises(ApplicationError) as refused:
        await activities.apply_card_resolution(resolving(auto=False))
    assert refused.value.type == CARD_NOT_PENDING_ERROR
    applied = await activities.apply_card_resolution(resolving(auto=True))
    assert (applied.resolved.status, applied.is_error) == ("approved", False)
    # Calder was listed but not suggested, so it wasn't sent.
    assert applied.content.startswith(
        "Sent without review (this chat's setting): the Harmony Network Request "
        f'"{brief_title(BRIEF)}" went to Northlake Characterization Core. Its ID, for '
        f"get_request_status only (never show IDs to Alex Chen): {request_id}.\n\n"
        "Listed but not sent: Calder Computational.\n\n"
        "Their progress shows on the request's card"
    )


async def test_a_card_that_couldnt_be_sent_says_nothing_went_out(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await seeded_world(db_sessionmaker)
    conversation_id = await start_chat(db_sessionmaker, world_id)
    card_id = await propose(db_sessionmaker, world_id, conversation_id, NORTHLAKE, auto=True)
    applied = await _offline(db_sessionmaker).apply_card_resolution(
        CardResolutionInput(
            world_id=world_id,
            conversation_id=conversation_id,
            resolution=CardResolution(
                card_id=card_id,
                decision="reject",
                member_id=ALEX,
                feedback="no recipient is left to send to",
                auto=True,
            ),
            request_id=uuid4(),
        )
    )
    assert (applied.resolved.status, applied.is_error) == ("rejected", True)
    assert applied.content == ("Couldn't send: no recipient is left to send to. Nothing went out.")


# --- Consult resolutions -------------------------------------------------------------


async def test_approving_a_consult_releases_the_draft_to_this_thread(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id, thread_id = await _thread_with_northlake(db_sessionmaker)
    escalation_id = await routed_consult(db_sessionmaker, world_id, thread_id)
    activities = _offline(db_sessionmaker)

    # Only Northlake's own people decide Northlake's question.
    with pytest.raises(ApplicationError) as failed:
        await activities.apply_resolution(
            _resolve(
                world_id, thread_id, escalation_id, "approve", member_id=ALEX, org_id=HENDERSON
            )
        )
    assert (failed.value.type, failed.value.non_retryable) == (NOT_IN_ORG_ERROR, True)

    arg = _resolve(world_id, thread_id, escalation_id, "approve")
    assert await activities.stored_resolution(arg) is None
    applied = await activities.apply_resolution(arg)
    # A retry after the commit, or a read-back, reports the same thing.
    assert await activities.apply_resolution(arg) == applied
    assert await activities.stored_resolution(arg) == applied

    released = answerer.result_text("approve", consult_draft().answer)
    assert (applied.side, applied.sent, applied.resolved.message_id) == (
        "responder",
        False,
        None,
    )
    assert applied.owed is not None
    assert (applied.owed.tool_use_id, applied.owed.content) == ("toolu_consult", released)
    consult = await _consult_of(db_sessionmaker, world_id, thread_id)
    assert (consult.status, consult.released_text) == ("released", released)
    assert (consult.released_by, consult.released_by_member_id) == ("member", HELEN)
    assert await _messages(db_sessionmaker, world_id, thread_id) == []
    async with db_sessionmaker() as session:
        escalation = await repo.get_required(session, Escalation, world_id, escalation_id)
    assert (escalation.status, escalation.resolved_by_member_id) == ("resolved", HELEN)

    with pytest.raises(ApplicationError) as failed:
        await activities.apply_resolution(
            _resolve(world_id, thread_id, escalation_id, "decline", text="No.")
        )
    assert failed.value.type == ESCALATION_NOT_OPEN_ERROR
    assert failed.value.details[0]["resolved_by_member_id"] == str(HELEN)


async def test_declining_a_consult_owes_what_the_representative_may_say(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id, thread_id = await _thread_with_northlake(db_sessionmaker)
    escalation_id = await routed_consult(db_sessionmaker, world_id, thread_id)
    activities = _offline(db_sessionmaker)
    arg = _resolve(world_id, thread_id, escalation_id, "decline", text="We can't meet Nov 6.")
    applied = await activities.apply_resolution(arg)
    assert await activities.apply_resolution(arg) == applied
    assert applied.owed is not None
    owed = answerer.result_text("decline", "We can't meet Nov 6.")
    assert applied.owed.content == owed
    assert not applied.sent
    # What the representative may say reached it, so the consult keeps it as it would
    # a release ("What our representative saw" lists it).
    consult = await _consult_of(db_sessionmaker, world_id, thread_id)
    assert (consult.status, consult.released_text) == ("declined", owed)
    assert (consult.released_by, consult.released_by_member_id) == ("member", HELEN)
    assert answerer.settled_result(consult) == owed
    assert await _messages(db_sessionmaker, world_id, thread_id) == []


async def test_taking_over_sends_the_members_own_words(db_sessionmaker: Sessionmaker) -> None:
    world_id, thread_id = await _thread_with_northlake(db_sessionmaker)
    escalation_id = await routed_consult(db_sessionmaker, world_id, thread_id)
    arg = _resolve(
        world_id, thread_id, escalation_id, "take_over", text="Helen here: we can do Nov 4."
    )
    applied = await _offline(db_sessionmaker).apply_resolution(arg)

    assert (applied.resolved.message_id, applied.sent) == (arg.message_id, True)
    assert applied.owed is not None
    # The representative is told what its side sent, quoted, so it doesn't contradict it.
    taken_over = answerer.result_text("take_over", "Helen here: we can do Nov 4.")
    assert applied.owed.content == taken_over
    assert taken_over == (
        f"{answerer.TAKEN_OVER} What they sent:\n<released_text>\n"
        "Helen here: we can do Nov 4.\n</released_text>"
    )
    (message,) = await _messages(db_sessionmaker, world_id, thread_id)
    assert (message.id, message.provenance, message.author_member_id, message.from_org_id) == (
        arg.message_id,
        "human",
        HELEN,
        NORTHLAKE,
    )
    assert message.body == "Helen here: we can do Nov 4."
    consult = await _consult_of(db_sessionmaker, world_id, thread_id)
    assert (consult.status, consult.released_text) == ("released", taken_over)


async def _two_revs(
    sessionmaker: Sessionmaker, world_id: UUID, thread_id: UUID
) -> tuple[UUID, UUID]:
    """A Northlake consult escalation routed to Raj with rev 1 and a redraft, rev 2
    (drawn on the internal reserve block), now current."""
    consult_id, escalation_id = uuid4(), uuid4()
    route = Route(assignees=[RouteAssignee(member="raj", why="Raj runs the XRD.")])
    async with unit_of_work(sessionmaker, world_id) as uow:
        await consults.open(
            uow,
            consult_id=consult_id,
            thread_id=thread_id,
            side="responder",
            org_id=NORTHLAKE,
            question="Before Nov 6?",
            turn_id=uuid4(),
            tool_use_id="toolu_c",
        )
        await escalations.open(
            uow,
            escalation_id=escalation_id,
            org_id=NORTHLAKE,
            kind="consult",
            dedupe_key=f"consult:{consult_id}",
            thread_id=thread_id,
            side="responder",
            consult_id=consult_id,
        )
        for rev, answer in ((1, "REV1: we can do Nov 12."), (2, "REV2: the reserve block Nov 4.")):
            draft = consult_draft(answer).model_copy(update={"route": route})
            await escalations.add_draft(uow, escalation_id, draft, turn_id=None, draft_rev=rev)
        await consults.mark_needs_human(uow, consult_id)
        await uow.commit()
    return consult_id, escalation_id


async def test_approving_an_older_draft_rev_releases_that_rev(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id, thread_id = await _thread_with_northlake(db_sessionmaker)
    consult_id, escalation_id = await _two_revs(db_sessionmaker, world_id, thread_id)
    activities = _offline(db_sessionmaker)

    with pytest.raises(ApplicationError) as failed:
        await activities.apply_resolution(
            _resolve(world_id, thread_id, escalation_id, "approve", member_id=RAJ, draft_rev=3)
        )
    assert failed.value.type == "ValueError"

    # Rev 1, as drafted, never rev 2's reserve block.
    await activities.apply_resolution(
        _resolve(world_id, thread_id, escalation_id, "approve", member_id=RAJ, draft_rev=1)
    )
    async with db_sessionmaker() as session:
        consult = await repo.get_required(session, Consult, world_id, consult_id)
    assert consult.released_text == answerer.result_text("approve", "REV1: we can do Nov 12.")
    assert consult.released_by_member_id == RAJ


# --- Held messages (Ask) -------------------------------------------------------------


async def _held(
    sessionmaker: Sessionmaker,
    world_id: UUID,
    thread_id: UUID,
    text: str,
) -> UUID:
    """Northlake's representative's `send_message` held for a teammate, as the
    representative's tool leaves it. Returns the escalation id."""
    message_id = uuid4()
    reason = "A teammate approves every message in Ask mode."
    async with unit_of_work(sessionmaker, world_id) as uow:
        escalation, _ = await escalations.open(
            uow,
            escalation_id=uuid5(message_id, "outbound_review"),
            org_id=NORTHLAKE,
            kind="outbound_review",
            dedupe_key=f"outbound:{message_id}",
            thread_id=thread_id,
            side="responder",
        )
        draft = ConsultDraft(
            answer=text, needs_human=True, needs_human_reason=reason, route=None, rationale=reason
        )
        await escalations.add_draft(uow, escalation.id, draft, turn_id=None, draft_rev=1)
        await routing.apply_route(uow, escalation, None)
        await threads.set_state(uow, thread_id, "awaiting_principal", side_to_move="responder")
        await uow.commit()
    return escalation.id


async def test_an_approved_held_message_goes_out_as_the_teammate_left_it(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id, thread_id = await _thread_with_northlake(db_sessionmaker)
    escalation_id = await _held(db_sessionmaker, world_id, thread_id, "We can run them Nov 4.")
    activities = _offline(db_sessionmaker)
    arg = _resolve(world_id, thread_id, escalation_id, "approve", text="We can run them Nov 12.")
    applied = await activities.apply_resolution(arg)
    assert await activities.apply_resolution(arg) == applied

    async with db_sessionmaker() as session:
        escalation = await repo.get_required(session, Escalation, world_id, escalation_id)
    sent_id = escalations.held_message_id(escalation)
    assert (applied.resolved.message_id, applied.sent) == (sent_id, True)
    assert applied.owed is not None
    # What crossed isn't what it wrote: it is told, quoted.
    assert (applied.owed.content, applied.owed.is_error) == (
        "Sent as your side edited it:\n<released_text>\nWe can run them Nov 12.\n</released_text>",
        False,
    )
    (message,) = await _messages(db_sessionmaker, world_id, thread_id)
    assert (message.id, message.kind, message.body) == (
        sent_id,
        "message",
        "We can run them Nov 12.",
    )
    assert (message.provenance, message.approved_by_member_id) == ("agent_approved", HELEN)


async def test_a_declined_held_message_sends_nothing(db_sessionmaker: Sessionmaker) -> None:
    world_id, thread_id = await _thread_with_northlake(db_sessionmaker)
    escalation_id = await _held(
        db_sessionmaker, world_id, thread_id, "Our other client booked Nov 4."
    )
    applied = await _offline(db_sessionmaker).apply_resolution(
        _resolve(world_id, thread_id, escalation_id, "decline", text="Don't name clients.")
    )
    assert (applied.resolved.message_id, applied.sent) == (None, False)
    assert applied.owed is not None
    assert applied.owed.is_error
    # The teammate's note is theirs, quoted like a declined consult's, never Harmony's.
    assert applied.owed.content == (
        "Not sent: your side didn't approve it. What you may say:\n"
        "<released_text>\nDon't name clients.\n</released_text>"
    )
    assert await _messages(db_sessionmaker, world_id, thread_id) == []


def _edit(world_id: UUID, body: str, escalation_id: UUID | None = None) -> SteerRequest:
    key = uuid4()
    return SteerRequest(
        steer_id=steers.steer_id_for(world_id, NORTHLAKE, HELEN, key),
        side="responder",
        org_id=NORTHLAKE,
        member_id=HELEN,
        client_key=key,
        body=body,
        escalation_id=escalation_id,
    )


async def test_a_steer_is_recorded_once_and_its_id_never_takes_another_body(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id, thread_id = await _thread_with_northlake(db_sessionmaker)
    activities = _offline(db_sessionmaker)
    edit = _edit(world_id, "Don't name other clients.")
    arg = RecordSteerInput(world_id=world_id, thread_id=thread_id, steer=edit)

    recorded = await activities.record_steer(arg)
    # A retried activity, or a retried POST's Update, finds the same row.
    assert await activities.record_steer(arg) == recorded
    async with db_sessionmaker() as session:
        helen = await repo.get_required(session, Member, world_id, HELEN)
    assert (recorded.seq, recorded.author_name) == (1, helen.name)
    other = edit.model_copy(update={"body": "Name them."})
    with pytest.raises(ApplicationError) as conflict:
        await activities.record_steer(arg.model_copy(update={"steer": other}))
    assert conflict.value.type == STEER_CONFLICT_ERROR


async def test_approving_a_rewritten_held_message_sends_that_rev_as_the_representatives(
    db_sessionmaker: Sessionmaker,
) -> None:
    """EDIT.md §3.5: the approval sends the rev the member saw, stamped with the
    representative's turn, and tells it what went out against its own rev 1."""
    world_id, thread_id = await _thread_with_northlake(db_sessionmaker)
    drafted, rewrite = "Our other client has Nov 4.", "Nov 4 is taken; Nov 10 is open."
    escalation_id = await _held(db_sessionmaker, world_id, thread_id, drafted)
    reason = "A teammate approves every message in Ask mode."
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        draft = ConsultDraft(
            answer=rewrite,
            needs_human=True,
            needs_human_reason=reason,
            route=None,
            rationale=reason,
        )
        await escalations.add_draft(uow, escalation_id, draft, turn_id=uuid4(), draft_rev=2)
        await uow.commit()

    applied = await _offline(db_sessionmaker).apply_resolution(
        _resolve(world_id, thread_id, escalation_id, "approve", draft_rev=2)
    )

    assert applied.owed is not None
    assert applied.owed.content == (
        f"Sent as your side edited it:\n<released_text>\n{rewrite}\n</released_text>"
    )
    (message,) = await _messages(db_sessionmaker, world_id, thread_id)
    # Rev 1's turn (none here), never the rewrite's.
    assert (message.body, message.turn_id) == (rewrite, None)


async def test_a_steer_settles_from_the_rows_its_parts_wrote_once(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id, thread_id = await _thread_with_northlake(db_sessionmaker)
    escalation_id = await _held(db_sessionmaker, world_id, thread_id, "Our client has Nov 4.")
    activities = _offline(db_sessionmaker)
    edit = _edit(world_id, "Don't mention clients.", escalation_id)
    await activities.record_steer(
        RecordSteerInput(world_id=world_id, thread_id=thread_id, steer=edit, draft_rev=2)
    )
    async with unit_of_work(db_sessionmaker, world_id) as uow:
        await steers.set_guidance_outcome(
            uow, edit.steer_id, outcome="updated", text="Don't mention other clients."
        )
        await uow.commit()

    # The guidance landed; the rewrite didn't, and the item is still open.
    settle = SettleSteerInput(world_id=world_id, steer_id=edit.steer_id, failure="model_error")
    # Its `seq`, which the workflow needs when `record_steer` failed after its commit.
    assert await activities.settle_steer(settle) == 1
    await activities.settle_steer(settle.model_copy(update={"failure": "thread_closed"}))
    assert await activities.settle_steer(settle.model_copy(update={"steer_id": uuid4()})) is None

    async with db_sessionmaker() as session:
        row = await repo.get_required(session, Steer, world_id, edit.steer_id)
    assert (row.status, row.guidance_outcome, row.guidance_version) == ("failed", "updated", 1)
    assert (row.draft_outcome, row.failure_reason) == ("failed", "model_error")


async def test_an_unedited_approval_says_only_that_it_was_sent(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id, thread_id = await _thread_with_northlake(db_sessionmaker)
    escalation_id = await _held(db_sessionmaker, world_id, thread_id, "We can run them Nov 4.")
    applied = await _offline(db_sessionmaker).apply_resolution(
        _resolve(world_id, thread_id, escalation_id, "approve", text="We can run them Nov 4.")
    )
    assert applied.owed is not None
    assert applied.owed.content == "Sent, approved by your side."


async def test_taking_over_a_held_message_tells_the_representative_what_was_sent(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id, thread_id = await _thread_with_northlake(db_sessionmaker)
    escalation_id = await _held(db_sessionmaker, world_id, thread_id, "$1,410, results by Nov 5.")
    applied = await _offline(db_sessionmaker).apply_resolution(
        _resolve(world_id, thread_id, escalation_id, "take_over", text="$1,200 <b>Nov 6</b>.")
    )
    assert applied.sent
    assert applied.owed is not None
    assert applied.owed.content == (
        f"Not sent: {answerer.TAKEN_OVER} What they sent:\n"
        "<released_text>\n$1,200 &lt;b>Nov 6&lt;/b>.\n</released_text>"
    )


# --- Thread endings ------------------------------------------------------------------


@pytest.mark.parametrize("ended", ["closed", "expired"])
async def test_an_ended_thread_cancels_its_open_items(
    db_sessionmaker: Sessionmaker, ended: Literal["closed", "expired"]
) -> None:
    world_id, thread_id = await _thread_with_northlake(db_sessionmaker)
    escalation_id = await routed_consult(db_sessionmaker, world_id, thread_id)
    activities = _offline(db_sessionmaker)
    end_reason: EndReason = "quiet" if ended == "closed" else "deadline"
    arg = ThreadStateInput(
        world_id=world_id, thread_id=thread_id, state=ended, end_reason=end_reason
    )
    for _ in range(2):
        await activities.set_thread_state(arg)
    async with db_sessionmaker() as session:
        thread = await repo.get_required(session, Thread, world_id, thread_id)
        escalation = await repo.get_required(session, Escalation, world_id, escalation_id)
    assert (thread.state, thread.side_to_move, thread.end_reason) == (ended, None, end_reason)
    assert escalation.status == "cancelled"
    assert await _notices(db_sessionmaker, world_id, thread_id) == []


async def test_reassigning_a_question_to_the_requester_refreshes_their_chat(
    db_sessionmaker: Sessionmaker,
) -> None:
    sm = db_sessionmaker
    world_id = await seeded_world(sm)
    outreach = await approved_outreach(sm, world_id, NORTHLAKE)
    thread_id = await open_thread(sm, outreach, NORTHLAKE)
    consult_id, escalation_id = uuid4(), uuid4()
    async with unit_of_work(sm, world_id) as uow:
        await consults.open(
            uow,
            consult_id=consult_id,
            thread_id=thread_id,
            side="requester",
            org_id=HENDERSON,
            question="Northlake proposes $1,410. Go ahead?",
            turn_id=uuid4(),
            tool_use_id="toolu_go_ahead",
        )
        escalation, _ = await escalations.open(
            uow,
            escalation_id=escalation_id,
            org_id=HENDERSON,
            kind="consult",
            dedupe_key=f"consult:{consult_id}",
            thread_id=thread_id,
            side="requester",
            consult_id=consult_id,
        )
        await routing.apply_route(
            uow, escalation, Route(assignees=[RouteAssignee(member="maya", why="Spending.")])
        )
        await uow.commit()

    async def chat_version() -> int:
        async with sm() as session:
            chat = await repo.get_required(
                session, Conversation, world_id, outreach.conversation_id
            )
            return chat.version

    before = await chat_version()
    moved = await _offline(sm).reassign_escalation(
        ReassignInput(
            world_id=world_id,
            reassignment=Reassignment(
                escalation_id=escalation_id, to_member_id=ALEX, by_member_id=MAYA
            ),
        )
    )
    assert moved.assignees == [ALEX]
    # Alex's chat refetches and shows the question inline.
    assert await chat_version() > before
    async with sm() as session:
        chat = await repo.get_required(session, Conversation, world_id, outreach.conversation_id)
        items = await conversations.chat_items(session, chat)
    inline = [item for item in items if isinstance(item, InlineQuestionItem)]
    assert [item.escalation_id for item in inline] == [escalation_id]


async def test_a_refusal_tells_the_other_side_once(db_sessionmaker: Sessionmaker) -> None:
    world_id, thread_id = await _thread_with_northlake(db_sessionmaker)
    activities = _offline(db_sessionmaker)
    arg = ThreadStateInput(
        world_id=world_id,
        thread_id=thread_id,
        state="closed",
        end_reason="stopped",
        closed_by="responder",
        last_turn=True,
        stop_reason="refused",
    )
    # A retry after the commit queues nothing more.
    for _ in range(2):
        await activities.set_thread_state(arg)
    notices = await _notices(db_sessionmaker, world_id, thread_id)
    assert [(side, kind) for side, kind, _ in notices] == [("requester", "closed")]
    async with db_sessionmaker() as session:
        thread = await repo.get_required(session, Thread, world_id, thread_id)
    assert (thread.state, thread.end_reason, thread.closed_by, thread.stop_reason) == (
        "closed",
        "stopped",
        "responder",
        "refused",
    )


async def test_a_failed_last_turn_tells_its_org_why_there_is_no_summary_once(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id, thread_id = await _thread_with_northlake(db_sessionmaker)
    activities = _offline(db_sessionmaker)
    await activities.set_thread_state(
        ThreadStateInput(
            world_id=world_id,
            thread_id=thread_id,
            state="closed",
            end_reason="stopped",
            stop_reason="demo_budget",
        )
    )
    turn_id = uuid4()
    arg = UnsummarizedInput(
        world_id=world_id,
        thread_id=thread_id,
        org_id=HENDERSON,
        turn_id=turn_id,
        reason="demo_budget",
    )
    # A retry after the commit writes nothing more.
    for _ in range(2):
        await activities.note_unsummarized(arg)
    async with db_sessionmaker() as session:
        ours = await notes.list_for(
            session, world_id=world_id, thread_id=thread_id, org_id=HENDERSON
        )
        theirs = await notes.list_for(
            session, world_id=world_id, thread_id=thread_id, org_id=NORTHLAKE
        )
    assert [(note.author_kind, note.body) for note in ours] == [
        (
            "claude",
            "Claude couldn't write a closing summary for our side: the demo's spend limit for "
            "live models was reached when its last turn ran. The other side had already "
            "closed the conversation.",
        )
    ]
    assert theirs == []


# --- Mode toward a counterparty ------------------------------------------------------


async def test_the_mode_is_the_counterpartys_own_else_the_default(
    db_sessionmaker: Sessionmaker,
) -> None:
    world_id = await seeded_world(db_sessionmaker)

    async def mode(org_id: UUID, counterparty: UUID) -> Autonomy:
        async with db_sessionmaker() as session:
            return await mode_toward(
                session, world_id=world_id, org_id=org_id, counterparty_org_id=counterparty
            )

    # Northlake trusts Ridgeline with more sharing but leaves its mode the default.
    assert await mode(NORTHLAKE, RIDGELINE) == "auto"
    assert await mode(NORTHLAKE, HENDERSON) == "auto"
    async with db_sessionmaker() as session, session.begin():
        session.add(
            TrustedOrganization(
                world_id=world_id,
                org_id=NORTHLAKE,
                counterparty_org_id=HENDERSON,
                autonomy="ask",
                sharing_description="",
            )
        )
    assert await mode(NORTHLAKE, HENDERSON) == "ask"
    async with db_sessionmaker() as session, session.begin():
        await session.execute(
            update(RepPolicy)
            .where(RepPolicy.world_id == world_id, RepPolicy.org_id == NORTHLAKE)
            .values(autonomy="skip", version=RepPolicy.version + 1)
        )
    # Read live: the default moved, the counterparty's own mode didn't.
    assert (await mode(NORTHLAKE, HENDERSON), await mode(NORTHLAKE, CALDER)) == ("ask", "skip")


# --- Workflow starts -----------------------------------------------------------------


async def test_opening_a_thread_starts_its_workflow_and_a_closed_request_wakes_the_chat(
    db_sessionmaker: Sessionmaker, temporal_env: WorkflowEnvironment
) -> None:
    client = temporal_env.client
    world_id = await seeded_world(db_sessionmaker)
    outreach = await approved_outreach(db_sessionmaker, world_id, NORTHLAKE)
    activities = NetworkActivities(client, db_sessionmaker)
    thread_id = uuid4()
    started = await activities.open_thread(
        OpenThreadInput(
            world_id=world_id,
            request_id=outreach.request_id,
            thread_id=thread_id,
            responder_org_id=NORTHLAKE,
        )
    )
    assert started.cassette_thread == thread_key("henderson", "northlake", 1)
    thread = client.get_workflow_handle(thread_workflow_id(world_id, thread_id))
    assert (await thread.describe()).status == WorkflowExecutionStatus.RUNNING
    async with db_sessionmaker() as session:
        row = await repo.get_required(session, Thread, world_id, thread_id)
        request = await repo.get_required(session, NetworkRequest, world_id, outreach.request_id)
    assert row.state == "active"
    # Every wait in the thread ends by then, inside the 24 h execution timeout.
    assert started.deadline_at == row.deadline_at == request.created_at + REQUEST_LIFETIME

    await activities.notify_conversation(
        NotifyConversationInput(
            world_id=world_id,
            conversation_id=outreach.conversation_id,
            update=NetworkUpdate(request_id=outreach.request_id, kind="closed"),
        )
    )
    chat = client.get_workflow_handle(conversation_workflow_id(world_id, outreach.conversation_id))
    assert (await chat.describe()).status == WorkflowExecutionStatus.RUNNING
    for handle in (thread, chat):
        await handle.terminate()
