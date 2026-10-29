"""A member's answer to a question their side's representative asked about a request
(requester side), given on its card in the chat (the resolve route) or told to Claude in
the chat (`answer_question`).

One answer, many askers. An approval may also go to the card's other open items routed
to the member (`also_for`: each gets a rev drafted from it, through the `use_answer`
Update, once this one is resolved). The answer goes only to the askers: the item
answered and those in `also_for`. When the chat's owner answers in their own words, the
card stores their organizations (`consults.add_goes_to`, for "Where your answer went"),
and the chat is told, so its Claude can ask whether part of the answer may go to the
request's other conversations. The answer mustn't name another organization on the
request: `card_answer` refuses it, as it does `also_for` on anything but the chat
owner's approval, in their own words, of an item routed to them, or with an item that
isn't theirs on the same card.

`waiting_on` lists the questions a chat's requests have waiting on a member, for Claude
in that chat to know of them and answer them with the member's words.

Owner: E4.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents import context
from harmony.agents.models import EscalationAction
from harmony.db import repo
from harmony.db.models import Consult, Escalation, NetworkRequest, Org, Thread
from harmony.domain import consults, requests


class AnswerRefused(ValueError):
    """An answer that can't go as given; the message says why, to the member."""


@dataclass(frozen=True, slots=True)
class CardAnswer:
    """The chat owner's answer, in their own words, to their side's question on a
    request: its card, and the organizations of the askers it goes to."""

    card: UUID
    request_id: UUID
    goes_to: list[UUID]


@dataclass(frozen=True, slots=True)
class Waiting:
    """A question waiting on a member: its open item, what it asks, and the
    conversation that asked it."""

    item: Escalation
    question: str
    thread: Thread


async def waiting_on(
    db: AsyncSession, *, world_id: UUID, conversation_id: UUID, member_id: UUID
) -> list[Waiting]:
    """The open questions on the chat's requests that its side's representative routed
    to `member_id`, oldest first: the cards waiting on them in the chat."""
    rows = await db.execute(
        select(Escalation, Consult.question, Thread)
        .join(
            Consult,
            (Consult.world_id == Escalation.world_id) & (Consult.id == Escalation.consult_id),
        )
        .join(
            Thread, (Thread.world_id == Escalation.world_id) & (Thread.id == Escalation.thread_id)
        )
        .join(
            NetworkRequest,
            (NetworkRequest.world_id == Thread.world_id) & (NetworkRequest.id == Thread.request_id),
        )
        .where(
            Escalation.world_id == world_id,
            Escalation.kind == "consult",
            Escalation.side == "requester",
            Escalation.status == "open",
            NetworkRequest.conversation_id == conversation_id,
        )
        .order_by(Escalation.created_at, Escalation.id)
    )
    return [
        Waiting(item=item, question=question, thread=thread)
        for item, question, thread in rows
        if str(member_id) in item.assignees
    ]


async def card_answer(
    db: AsyncSession,
    escalation: Escalation,
    *,
    member_id: UUID,
    org_id: UUID,
    action: EscalationAction,
    text: str | None,
    also_for: Sequence[UUID] = (),
) -> CardAnswer | None:
    """The chat owner's answer to a requester-side question, once `also_for` and the
    answer's names are checked (module docstring); None for any other resolution.
    Raises `AnswerRefused`."""
    world_id = escalation.world_id
    asked = escalation.kind == "consult" and escalation.side == "requester"
    if not asked or escalation.consult_id is None or escalation.thread_id is None:
        if also_for:
            raise AnswerRefused("only your side's own questions on a request share an answer")
        return None
    words = (text or "").strip()
    own_words = action == "approve" and bool(words)
    if also_for and not own_words:
        raise AnswerRefused("only an answer in your own words goes to other conversations")
    thread = await repo.get_required(db, Thread, world_id, escalation.thread_id)
    request = await repo.get_required(db, NetworkRequest, world_id, thread.request_id)
    owner = member_id == request.created_by_member_id
    mine = str(member_id) in escalation.assignees
    if also_for and not (owner and mine):
        raise AnswerRefused(
            "only the chat's owner shares an answer, from a question routed to them"
        )
    consult = await consults.get(db, world_id=world_id, consult_id=escalation.consult_id)
    card = consults.card_of(consult)
    goes_to = [thread.responder_org_id]
    if also_for:
        askers = {
            item.id: item for item in await consults.card_askers(db, world_id, card, member_id)
        }
        strays = [item_id for item_id in also_for if item_id not in askers]
        if escalation.id in also_for or strays:
            raise AnswerRefused("also_for takes only your own open items on this card")
        for item_id in also_for:
            other_id = askers[item_id].thread_id
            assert other_id is not None
            other = await repo.get_required(db, Thread, world_id, other_id)
            goes_to.append(other.responder_org_id)
    recipients = [row.org_id for row in requests.recipients_of(request)]
    if own_words:
        orgs = {org.id: org.name for org in await repo.list_in_world(db, Org, world_id)}
        # Short names count ("Northlake"), unless the shown asker's or ours starts the same.
        kept = (orgs[thread.responder_org_id], orgs[org_id])
        others = [orgs[other] for other in recipients if other != thread.responder_org_id]
        named = [name for name in others if context.names(words, name, keep=kept)]
        if named:
            # The asker shown gets the words as written (DESIGN §2, D2).
            leave = "Leave the name out." if len(named) == 1 else "Leave the names out."
            raise AnswerRefused(
                f"Your answer names {' and '.join(named)}, and "
                f"{orgs[thread.responder_org_id]} gets your words as written. {leave}"
            )
    if not (owner and own_words):
        return None
    return CardAnswer(card=card, request_id=request.id, goes_to=goes_to)


__all__ = ["AnswerRefused", "CardAnswer", "Waiting", "card_answer", "waiting_on"]
