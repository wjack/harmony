"""Giving a member's answer to their side's question on a request (`domain.answers`):
on its card (the resolve route) or told to Claude in the chat (`ChatAnswers`, behind
`answer_question`). Both resolve the item on its thread (`resolve_escalation`), then
`pass_on` the answer: the card's record of where it went, the other askers it also
serves (`use_answer`), and the chat's news that the owner answered.

Owner: E4.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError

from harmony.agents.models import EscalationResolution
from harmony.db.models import Escalation
from harmony.db.uow import unit_of_work
from harmony.domain import consults
from harmony.domain.answers import AnswerRefused, CardAnswer
from harmony.temporal.activities.network import nudge_request_chat, run_update, workflow_missing
from harmony.temporal.workflows.payloads import (
    ESCALATION_NOT_OPEN_ERROR,
    AnswerUsed,
    EscalationResolved,
    NetworkNews,
    UseAnswer,
    thread_workflow_id,
)

log = logging.getLogger(__name__)

NOT_OPEN = "That question isn't waiting any more: it was answered, or its conversation ended."


async def pass_on(
    client: Client,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    world_id: UUID,
    answered: UUID,
    answer: CardAnswer | None,
    also: Sequence[Escalation],
) -> None:
    """After the item `answered` is resolved: store where the owner's answer went, give
    it to the card's other askers in `also`, and tell the chat. An asker that won't take
    it stays open for a person, and the card shows it so."""
    # Stored once the answer is given, so a refused one never changes the card.
    if answer is not None:
        async with unit_of_work(sessionmaker, world_id) as uow:
            await consults.add_goes_to(uow, answer.card, answer.goes_to)
            await uow.commit()
    for item in also:
        assert item.thread_id is not None
        handle = client.get_workflow_handle(thread_workflow_id(world_id, item.thread_id))
        use = UseAnswer(escalation_id=item.id, from_escalation_id=answered)
        try:
            await run_update(handle, "use_answer", use, AnswerUsed)
        except (WorkflowUpdateFailedError, RPCError) as refused:
            log.info("escalation %s didn't take the answer: %s", item.id, refused)
    if answer is not None:
        # Best effort: the answer is given either way.
        try:
            await nudge_request_chat(
                client,
                sessionmaker,
                world_id=world_id,
                news=NetworkNews(request_id=answer.request_id, answered=True),
            )
        except Exception:
            log.warning("couldn't tell the chat of request %s", answer.request_id, exc_info=True)


class ChatAnswers:
    """`answer_question`'s delivery: the member's words from the chat, given as the
    card's Send answer gives them. Raises `AnswerRefused` when the thread won't take
    them."""

    def __init__(self, client: Client, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._client = client
        self._sessionmaker = sessionmaker

    async def give(
        self,
        world_id: UUID,
        resolution: EscalationResolution,
        *,
        thread_id: UUID,
        answer: CardAnswer | None,
        also: Sequence[Escalation],
    ) -> None:
        handle = self._client.get_workflow_handle(thread_workflow_id(world_id, thread_id))
        try:
            await run_update(handle, "resolve_escalation", resolution, EscalationResolved)
        except WorkflowUpdateFailedError as failed:
            cause = failed.cause
            if isinstance(cause, ApplicationError) and cause.type == ESCALATION_NOT_OPEN_ERROR:
                raise AnswerRefused(NOT_OPEN) from None
            raise AnswerRefused(
                cause.message if isinstance(cause, ApplicationError) else str(failed)
            ) from None
        except RPCError as error:
            if not workflow_missing(error):
                raise
            raise AnswerRefused(NOT_OPEN) from None
        await pass_on(
            self._client,
            self._sessionmaker,
            world_id=world_id,
            answered=resolution.escalation_id,
            answer=answer,
            also=also,
        )


__all__ = ["NOT_OPEN", "ChatAnswers", "pass_on"]
