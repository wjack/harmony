"""The representative's tools: the same three for every org (REVISION_UNSTRUCTURED.md §5).

- `send_message(text)`: commits a `thread_messages` row (provenance `agent`), and the
  turn goes on: a turn may send several messages. In *Ask* (the side's mode toward the
  counterparty, read live at each call) the message is held instead:
  an `outbound_review` escalation whose draft is the message; the thread waits
  `awaiting_principal`, the turn stops, and the message goes out (provenance
  `agent_approved`, id `escalations.held_message_id`) once a teammate approves it.
- `consult(question)`: opens a consult (`domain.consults.open`) and stops the turn until
  an answer is released; the tool result is the released answer. The counterparty hears
  nothing.
- `close(summary)`: ends the thread for both sides. The summary is this side's
  `rep_sessions.close_summary`, for its own org only and never sent; a `close` message
  with an empty body marks the point in the thread, and the counterparty's
  representative gets a `closed` notice. On a thread the counterparty closed, `close`
  only stores this side's summary (its one last turn). A `close` is never held, even
  in *Ask*: nothing it writes reaches the counterparty but the marker. It is refused
  while the counterparty has sent no message: a closed thread can't be reopened, and
  the way to wait for a reply is to end the turn (LIVE_DEMO_PLAN 3.13 E1). Before this
  side has sent one either (its opener declined, say), there is no reply to wait for:
  the refusal says that ending the turn without sending ends the conversation.

Every call first checks, under the thread's row lock, that this side may act now: the
thread is live, nothing of this side's waits on a person, and it is this side's turn
(the turn's own earlier messages hand the move to the counterparty, which doesn't end
the turn). Otherwise the call is an error result and writes nothing. A call an earlier
attempt of the same turn already applied (its side-effect id exists) returns what it
applied then: a retried turn never decides again, so a mode changed in between can't
send a held message or hold a sent one. Calls run one at a time
(`disable_parallel_tool_use`).

A response with text and no tool call is a `send_message` only when it answers the
turn's opening (the runtime passes no other to `text_reply`), the turn has sent nothing
yet and it had something to answer (`RepToolScope.bare_text_sends`): the model answered
in prose instead of calling the tool. Other text, after a refused `close` say, is the
model's own and stays in its transcript.

Owner: R-agents (the tool names, schemas and input models are the contract's).
"""

from __future__ import annotations

from typing import Final
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.agents import context
from harmony.agents.consult import ConsultDraft
from harmony.agents.runtime import (
    ConsultOpened,
    MessageHeld,
    MessageSent,
    ToolCall,
    ToolExecution,
)
from harmony.agents.tools import ToolDefinition
from harmony.db import repo
from harmony.db.models import Consult, Escalation, Org, Thread, ThreadMessage
from harmony.db.models.choices import Autonomy, EscalationKind, Side
from harmony.db.uow import UnitOfWork, unit_of_work
from harmony.domain import consults, escalations, routing, settings, threads
from harmony.ids import side_effect_id

SEND_MESSAGE: Final = "send_message"
CONSULT: Final = "consult"
CLOSE: Final = "close"

SEND_MESSAGE_TOOL: Final[ToolDefinition] = {
    "name": SEND_MESSAGE,
    "description": (
        "Send a message to the counterparty's representative. They read the text exactly "
        "as written. Never send notes about the conversation (status, 'awaiting your "
        "reply', who sent it). You may send more than one in a turn; your turn ends when "
        "you stop calling tools, and then they reply."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The message, exactly as the counterparty will read it.",
            }
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}

CONSULT_TOOL: Final[ToolDefinition] = {
    "name": CONSULT,
    "description": (
        "Ask your own side for something you don't have: a fact, a price, an exception, "
        "or a decision. You wait for the answer, which comes back as this tool's result; "
        "the counterparty hears nothing unless you tell them."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "A self-contained question: what you need and why, with the details "
                    "of the conversation that matter."
                ),
            }
        },
        "required": ["question"],
        "additionalProperties": False,
    },
}

CLOSE_TOOL: Final[ToolDefinition] = {
    "name": CLOSE,
    "description": (
        "End the conversation for good, for both sides, once it's settled or clearly "
        "going nowhere. Not for waiting: to wait for their reply, stop calling tools. "
        "The summary is for your own organization only and is never sent to the "
        "counterparty."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "What happened and what was agreed, for your organization.",
            }
        },
        "required": ["summary"],
        "additionalProperties": False,
    },
}

# The frozen order of every representative's tool list.
REP_TOOLS: Final[tuple[ToolDefinition, ...]] = (SEND_MESSAGE_TOOL, CONSULT_TOOL, CLOSE_TOOL)


# --- Input models ---------------------------------------------------------------------


class _Input(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SendMessageInput(_Input):
    text: str = Field(min_length=1)


class ConsultInput(_Input):
    question: str = Field(min_length=1)


class CloseInput(_Input):
    summary: str = Field(min_length=1)


REP_INPUTS: Final[dict[str, type[_Input]]] = {
    SEND_MESSAGE: SendMessageInput,
    CONSULT: ConsultInput,
    CLOSE: CloseInput,
}


# --- Executor ---------------------------------------------------------------------------

# A bare text reply is `send_message` under this pseudo tool_use_id (its side-effect id).
TEXT_REPLY_ID: Final = "text"
# The `send_message` result: how to wait, since a bare "Sent." reads as a cue to say more.
SENT: Final = (
    "Sent. {counterparty} reads it exactly as written and replies after your turn ends. "
    "To wait for their reply, end your turn now: stop calling tools."
)
CLOSED: Final = "Closed. Nothing more reaches the counterparty."
# A `close` before the counterparty has written anything.
NOT_REPLIED: Final = (
    "{counterparty} hasn't replied yet. To wait for their reply, end your turn: stop "
    "calling tools. `close` ends the conversation for good."
)
# A `close` before either side has written anything: the thread expires once this
# turn ends with nothing sent (`ThreadWorkflow`).
NOTHING_SENT: Final = (
    "Nothing from you has reached {counterparty}, so there is no conversation to close. "
    "If you end your turn without sending, it ends there. To reach them, use "
    "`send_message`."
)
# The held draft's reason (the inbox derives its own "Why this needs you").
_ASK_HOLD: Final = "A teammate approves every message in Ask mode."
_HELD_KIND: Final[EscalationKind] = "outbound_review"


class RepToolScope(_Input):
    """Who is acting, where: one representative turn."""

    world_id: UUID
    thread_id: UUID
    side: Side
    org_id: UUID
    counterparty_org_id: UUID
    turn_id: UUID
    # See `TurnContext.bare_text_sends`.
    bare_text_sends: bool = True


class _Refused(Exception):
    """A call the model may correct: becomes an error `tool_result`, writes nothing."""


def _error(text: str) -> ToolExecution:
    return ToolExecution(result=text, is_error=True)


class RepToolExecutor:
    """`runtime.ToolExecutor` for a representative turn.

    Each call runs in its own unit of work: it commits everything or, for an error
    result, nothing. IDs are `ids.side_effect_id(turn_id, step_idx, tool_use_id)`, so a
    retried turn finds the rows it wrote.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], scope: RepToolScope) -> None:
        self._sessionmaker = sessionmaker
        self._scope = scope

    async def execute(self, call: ToolCall, *, step_idx: int) -> ToolExecution:
        input_model = REP_INPUTS.get(call.name)
        if input_model is None:
            return _error(f"There is no tool named {call.name}.")
        try:
            args = input_model.model_validate(call.input)
        except ValidationError as error:
            return _error(f"Invalid input for {call.name}: {_first_error(error)}")
        effect_id = side_effect_id(self._scope.turn_id, step_idx, call.id)
        try:
            async with unit_of_work(self._sessionmaker, self._scope.world_id) as uow:
                # The row lock holds the checks, and the mode read, until commit.
                thread = await threads.locked(uow, self._scope.thread_id)
                execution = await self._replayed(uow.session, effect_id)
                if execution is None:
                    await self._check_turn(uow.session, thread, args)
                    execution = await self._run(uow, thread, args, effect_id, call.id)
                await uow.commit()
        except _Refused as refused:
            return _error(str(refused))
        return execution

    async def text_reply(self, text: str, *, step_idx: int) -> ToolExecution | None:
        """A bare text reply is a `send_message` when the turn has sent nothing else and
        had something to answer (module docstring)."""
        scope = self._scope
        if not scope.bare_text_sends:
            return None
        effect_id = side_effect_id(scope.turn_id, step_idx, TEXT_REPLY_ID)
        async with self._sessionmaker() as session:
            sent = await session.scalars(
                select(ThreadMessage.id).where(
                    ThreadMessage.world_id == scope.world_id,
                    ThreadMessage.thread_id == scope.thread_id,
                    ThreadMessage.turn_id == scope.turn_id,
                )
            )
            earlier = [message_id for message_id in sent if message_id != effect_id]
        if earlier:
            return None
        call = ToolCall(id=TEXT_REPLY_ID, name=SEND_MESSAGE, input={"text": text.strip()})
        return await self.execute(call, step_idx=step_idx)

    async def _replayed(self, session: AsyncSession, effect_id: UUID) -> ToolExecution | None:
        """What an earlier attempt of this call applied, as it returned it then; None
        when nothing was applied yet."""
        world_id = self._scope.world_id
        message = await repo.get(session, ThreadMessage, world_id, effect_id)
        if message is not None:
            if message.kind == "close":
                return _closed(message)
            return _sent(message, await self._counterparty(session))
        held = await repo.get(session, Escalation, world_id, _held_id(effect_id))
        if held is not None:
            return _held(held)
        consult = await repo.get(session, Consult, world_id, effect_id)
        if consult is not None:
            return _consulting(consult)
        return None

    async def _check_turn(self, session: AsyncSession, thread: Thread, args: _Input) -> None:
        """`_Refused` unless this side may act on the locked thread now (module
        docstring)."""
        scope = self._scope
        if thread.state in threads.TERMINAL_STATES:
            if thread.state == "closed" and isinstance(args, CloseInput):
                return
            raise _Refused(
                f"This conversation has ended ({thread.state}). Nothing more reaches the "
                "counterparty."
            )
        waiting = thread.state == "awaiting_principal" or await _open_escalation(
            session, scope.world_id, thread_id=thread.id, org_id=scope.org_id
        )
        if waiting:
            raise _Refused(
                "The conversation is waiting on a person on your side. Nothing can be sent "
                "until they decide; end your turn."
            )
        if thread.side_to_move not in (None, scope.side) and not await _sent_this_turn(
            session, scope.world_id, thread_id=thread.id, turn_id=scope.turn_id
        ):
            raise _Refused("It isn't your turn. Wait for the counterparty's reply.")
        if isinstance(args, CloseInput) and not await _has_written(
            session, scope.world_id, thread_id=thread.id, org_id=scope.counterparty_org_id
        ):
            wrote = await _has_written(
                session, scope.world_id, thread_id=thread.id, org_id=scope.org_id
            )
            refusal = NOT_REPLIED if wrote else NOTHING_SENT
            raise _Refused(refusal.format(counterparty=await self._counterparty(session)))

    async def _counterparty(self, session: AsyncSession) -> str:
        """The counterparty's name, as the representative's prompt gives it."""
        scope = self._scope
        org = await repo.get_required(session, Org, scope.world_id, scope.counterparty_org_id)
        return " ".join(org.name.split())

    async def _mode(self, session: AsyncSession) -> Autonomy:
        """The side's mode toward the counterparty, read live at the decision (§3)."""
        scope = self._scope
        return await settings.mode_toward(
            session,
            world_id=scope.world_id,
            org_id=scope.org_id,
            counterparty_org_id=scope.counterparty_org_id,
        )

    async def _run(
        self, uow: UnitOfWork, thread: Thread, args: _Input, effect_id: UUID, tool_use_id: str
    ) -> ToolExecution:
        match args:
            case SendMessageInput(text=text):
                return await self._send(uow, text, effect_id)
            case ConsultInput(question=question):
                return await self._consult(uow, question, effect_id, tool_use_id)
            case CloseInput(summary=summary):
                return await self._close(uow, thread, summary, effect_id)
            case _:
                raise _Refused("That tool isn't available.")

    # --- Messages -------------------------------------------------------------------

    async def _send(self, uow: UnitOfWork, text: str, message_id: UUID) -> ToolExecution:
        scope = self._scope
        if await self._mode(uow.session) == "ask":
            return await self._hold(uow, text, message_id)
        message = await threads.append_message(
            uow,
            scope.thread_id,
            message_id=message_id,
            from_org_id=scope.org_id,
            kind="message",
            body=text,
            provenance="agent",
            turn_id=scope.turn_id,
        )
        return _sent(message, await self._counterparty(uow.session))

    async def _close(
        self, uow: UnitOfWork, thread: Thread, summary: str, message_id: UUID
    ) -> ToolExecution:
        scope = self._scope
        session = uow.session
        if thread.state == "closed":
            # The counterparty closed it: only the summary is left to write.
            await threads.store_close_summary(uow, scope.thread_id, scope.side, summary)
            return ToolExecution(ends_turn=True, owed_result=CLOSED)
        await threads.store_close_summary(uow, scope.thread_id, scope.side, summary)
        message = await threads.append_message(
            uow,
            scope.thread_id,
            message_id=message_id,
            from_org_id=scope.org_id,
            kind="close",
            body="",
            provenance="agent",
            turn_id=scope.turn_id,
        )
        await threads.set_state(uow, scope.thread_id, "closed")
        org = await repo.get_required(session, Org, scope.world_id, scope.org_id)
        await context.queue_notice(
            uow,
            thread_id=scope.thread_id,
            side=threads.other(scope.side),
            kind="closed",
            text=context.closed_notice(org.name),
        )
        return _closed(message)

    async def _consult(
        self, uow: UnitOfWork, question: str, consult_id: UUID, tool_use_id: str
    ) -> ToolExecution:
        scope = self._scope
        consult, created = await consults.open(
            uow,
            consult_id=consult_id,
            thread_id=scope.thread_id,
            side=scope.side,
            org_id=scope.org_id,
            question=question,
            turn_id=scope.turn_id,
            tool_use_id=tool_use_id,
        )
        # The same question while it is open: its answer is owed to this call too.
        return _consulting(consult, deduplicated=not created)

    # --- Held for a person --------------------------------------------------------------

    async def _hold(self, uow: UnitOfWork, text: str, message_id: UUID) -> ToolExecution:
        """Hold a message for a teammate (*Ask*): an `outbound_review` escalation whose
        draft is the message, sent once approved (`escalations.held_message_id`). The
        turn stops; its result is owed then."""
        scope = self._scope
        escalation, created = await escalations.open(
            uow,
            escalation_id=_held_id(message_id),
            org_id=scope.org_id,
            kind=_HELD_KIND,
            dedupe_key=f"outbound:{message_id}",
            thread_id=scope.thread_id,
            side=scope.side,
        )
        if created and escalation.current_draft_rev is None:
            draft = ConsultDraft(
                answer=text,
                needs_human=True,
                needs_human_reason=_ASK_HOLD,
                route=None,
                rationale=_ASK_HOLD,
            )
            await escalations.add_draft(
                uow, escalation.id, draft, turn_id=scope.turn_id, draft_rev=1
            )
            await routing.apply_route(uow, escalation, None)
        await threads.set_state(uow, scope.thread_id, "awaiting_principal", side_to_move=scope.side)
        return _held(escalation)


def _held_id(message_id: UUID) -> UUID:
    """The escalation that holds a call's message for a person."""
    return uuid5(message_id, _HELD_KIND)


def _sent(message: ThreadMessage, counterparty: str) -> ToolExecution:
    return ToolExecution(
        result=SENT.format(counterparty=counterparty),
        effect=MessageSent(message_id=message.id, message_kind=message.kind),
    )


def _closed(message: ThreadMessage) -> ToolExecution:
    return ToolExecution(
        ends_turn=True,
        owed_result=CLOSED,
        effect=MessageSent(message_id=message.id, message_kind=message.kind),
    )


def _held(escalation: Escalation) -> ToolExecution:
    return ToolExecution(ends_turn=True, effect=MessageHeld(escalation_id=escalation.id))


def _consulting(consult: Consult, *, deduplicated: bool = False) -> ToolExecution:
    return ToolExecution(
        ends_turn=True,
        effect=ConsultOpened(consult_id=consult.id, deduplicated=deduplicated),
    )


async def _sent_this_turn(
    session: AsyncSession, world_id: UUID, *, thread_id: UUID, turn_id: UUID
) -> bool:
    """Whether this turn already sent a message: that handed the move over, and the turn
    may go on sending."""
    found = await session.scalar(
        select(ThreadMessage.id)
        .where(
            ThreadMessage.world_id == world_id,
            ThreadMessage.thread_id == thread_id,
            ThreadMessage.turn_id == turn_id,
        )
        .limit(1)
    )
    return found is not None


async def _has_written(
    session: AsyncSession, world_id: UUID, *, thread_id: UUID, org_id: UUID
) -> bool:
    """Whether `org_id` has sent a message on the thread (a close marker isn't one)."""
    found = await session.scalar(
        select(ThreadMessage.id)
        .where(
            ThreadMessage.world_id == world_id,
            ThreadMessage.thread_id == thread_id,
            ThreadMessage.from_org_id == org_id,
            ThreadMessage.kind == "message",
        )
        .limit(1)
    )
    return found is not None


async def _open_escalation(
    session: AsyncSession, world_id: UUID, *, thread_id: UUID, org_id: UUID
) -> bool:
    """Whether a person on `org_id`'s side is deciding something on the thread."""
    found = await session.scalar(
        select(Escalation.id)
        .where(
            Escalation.world_id == world_id,
            Escalation.thread_id == thread_id,
            Escalation.org_id == org_id,
            Escalation.status == "open",
        )
        .limit(1)
    )
    return found is not None


def _first_error(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"])
    return f"{where}: {first['msg']}" if where else first["msg"]


__all__ = [
    "CLOSE",
    "CLOSED",
    "CONSULT",
    "NOTHING_SENT",
    "NOT_REPLIED",
    "REP_INPUTS",
    "REP_TOOLS",
    "SEND_MESSAGE",
    "SENT",
    "TEXT_REPLY_ID",
    "CloseInput",
    "ConsultInput",
    "RepToolExecutor",
    "RepToolScope",
    "SendMessageInput",
]
