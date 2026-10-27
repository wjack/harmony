"""A steer's model calls: the guidance a member's Edit asks for, and the rewrite of a
held message (docs/design/EDIT.md §3.3, §3.5).

1. `write_guidance`: the org's internal Claude writes the guidance its representative
   follows for the rest of the thread, from prompts/steer_guidance.md. Its only inputs
   are members' words (the instruction, and the current guidance, written the same way)
   and code-written names: no brief, sharing text, instructions, records, thread or
   draft, so guidance carries nothing a member didn't type (EDIT.md §6).
   `parse_guidance` then decides, in order: a refusal or an unusable output is
   rejected; so is guidance over `steers.GUIDANCE_MAX_CHARS`, or guidance that adds a
   number, date, email, link or name none of those words hold (`steers.added_details`);
   text identical to the current version is `unchanged`; anything else is `updated`.
   A rejection is an outcome, not an error: a checkpointed response replays the same
   text on every retry.
2. `rewrite_held`: one call with no tools rewrites a message the representative wrote
   and its org holds for a teammate, from prompts/held_rewrite.md: the thread (the
   counterparty's words through `context.wrap_counterparty`), the current guidance, the
   held text (or the member's unsaved wording of it) and the instruction. A person
   approves the result before anything is sent.

Both are role `steer`, keyed (scenario, org, `steer`, thread, the steer's `seq`, step 0
for the guidance, 1 for the rewrite), and checkpointed on `keys.steer_guidance_turn_id`
and `keys.steer_rewrite_turn_id`. A consult draft's rewrite is the answerer's own
redraft (`consult.answer` with the instruction), not a call here.

Owner: R-agents.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.agents import context, keys
from harmony.agents.consult import failed_output, first_error, output_text, single_call
from harmony.agents.prompts import render
from harmony.agents.runtime import CheckpointedModel
from harmony.db import repo
from harmony.db.models import Escalation, Member, Org, Steer, Thread, ThreadMessage, World
from harmony.db.models.choices import SteerFailure
from harmony.domain import escalations, reopens, steers, threads
from harmony.llm import CallKey, ModelRequest
from harmony.llm.requests import Tier
from harmony.llm.wire import JsonObject
from harmony.names import first_name

# Adaptive thinking shares this with the JSON; the output is a few short lines.
STEER_MAX_TOKENS: Final = 8_000
GUIDANCE_STEP: Final = 0
REWRITE_STEP: Final = 1
# What the guidance writer is told when a waiting draft is rewritten beside it.
WAITING: Final = (
    "A draft from your side is waiting for a teammate and is being rewritten separately. "
    "Put in the guidance only what should hold for the rest of the conversation, not "
    "wording for that draft."
)
# What it is told when the steer reopens a closed conversation (EDIT.md §3.9).
REOPENING: Final = (
    "The conversation closed ({reason}). {member_first} is reopening it, and your "
    "representative will write the next message. Make the guidance say what to pick up."
)
NO_GUIDANCE: Final = "None yet."

STEER_GUIDANCE_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {"guidance": {"type": "string"}},
    "required": ["guidance"],
    "additionalProperties": False,
}
HELD_REWRITE_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
    "additionalProperties": False,
}

GuidanceOutcome = Literal["updated", "unchanged", "rejected"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _GuidanceOutput(_Model):
    guidance: str


class _RewriteOutput(_Model):
    message: str


class GuidanceParsed(_Model):
    """What the guidance call came to."""

    outcome: GuidanceOutcome
    # The new guidance, when `updated` (empty: the org withdrew it).
    text: str | None = None
    # Why it was rejected.
    failure: SteerFailure | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class _Parties:
    """A steer, its thread and the names both calls use."""

    scenario: str
    steer: Steer
    thread: Thread
    org: Org
    counterparty: Org
    author: Member

    @property
    def tier(self) -> Tier:
        return "featured" if self.org.featured else "background"

    def names(self) -> tuple[str, str]:
        return " ".join(self.org.name.split()), " ".join(self.counterparty.name.split())

    def side_line(self) -> str:
        org_name, counterparty_name = self.names()
        if self.steer.side == "requester":
            return f"it is asking {counterparty_name} for something on {org_name}'s behalf"
        return f"{counterparty_name} came to {org_name} with a request"

    def instruction(self) -> str:
        return context.quoted("instruction", self.steer.body, from_=self.author.name)

    def key(self, cassette_thread: str, *, turn_id: UUID, step: int, attempt: int) -> CallKey:
        return CallKey(
            world_id=self.steer.world_id,
            scenario=self.scenario,
            org=self.org.slug,
            role="steer",
            thread=cassette_thread,
            turn=self.steer.seq,
            turn_id=turn_id,
            step_idx=step,
            attempt=attempt,
        )


# --- Guidance -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GuidanceInputs:
    """The guidance call's prompt, and what its output is checked against."""

    prompt: str
    # The current guidance's text ("" for none).
    current: str
    # The texts guidance may take details from (`steers.added_details`).
    allowed: tuple[str, ...]
    names: tuple[str, ...]


async def guidance_inputs(session: AsyncSession, steer: Steer) -> GuidanceInputs:
    """The guidance prompt for `steer`: members' words and code-written names only. A
    reopen steer's says why the conversation closed, from the thread row, which holds
    the last run's ending until the reopen lands."""
    parties = await _parties(session, steer)
    return await _guidance_inputs(session, parties)


async def _guidance_inputs(session: AsyncSession, parties: _Parties) -> GuidanceInputs:
    steer = parties.steer
    org_name, counterparty_name = parties.names()
    current = await steers.latest_guidance(session, steer.world_id, steer.thread_id, steer.side)
    current_text = current.text if current is not None else ""
    member_first = first_name(parties.author.name) or "Your teammate"
    reopen = ""
    if steer.kind == "reopen":
        thread = parties.thread
        reason = reopens.end_line(
            thread.end_reason,
            side=steer.side,
            closed_by=thread.closed_by,
            stop_reason=thread.stop_reason,
            counterparty=counterparty_name,
        )
        reopen = REOPENING.format(reason=reason, member_first=member_first)
    prompt = render(
        "steer_guidance",
        {
            "org_name": org_name,
            "counterparty_name": counterparty_name,
            "side_line": parties.side_line(),
            "member_first": member_first,
            "guidance": context.quoted("guidance", current_text, from_=org_name)
            if current_text.strip()
            else NO_GUIDANCE,
            "instruction": parties.instruction(),
            "waiting": WAITING if steer.draft_rev is not None else "",
            "reopen": reopen,
        },
    )
    return GuidanceInputs(
        prompt=prompt,
        current=current_text,
        allowed=(steer.body, current_text, org_name, counterparty_name),
        names=tuple(await steers.world_names(session, steer.world_id)),
    )


def guidance_request(
    key: CallKey, *, tier: Tier, prompt: str, model_override: str | None = None
) -> ModelRequest:
    """The guidance call: the prompt alone, no tools, the guidance as the output."""
    return single_call(
        key,
        tier=tier,
        prompt=prompt,
        max_tokens=STEER_MAX_TOKENS,
        output_schema=STEER_GUIDANCE_SCHEMA,
        model_override=model_override,
    )


def parse_guidance(
    message: Mapping[str, Any], *, current: str, allowed: Sequence[str], names: Sequence[str]
) -> GuidanceParsed:
    """The guidance in the writer's response, checked in the module docstring's order."""
    failed = failed_output(message, who="the guidance writer")
    if failed is not None:
        failure: SteerFailure = "refused" if failed.reason == "refusal" else "invalid"
        return GuidanceParsed(outcome="rejected", failure=failure, detail=failed.detail)
    try:
        text = _GuidanceOutput.model_validate_json(output_text(message)).guidance.strip()
    except ValidationError as error:
        return GuidanceParsed(outcome="rejected", failure="invalid", detail=first_error(error))
    if len(text) > steers.GUIDANCE_MAX_CHARS:
        return GuidanceParsed(
            outcome="rejected",
            failure="invalid",
            detail=f"guidance is {len(text)} characters, over {steers.GUIDANCE_MAX_CHARS}",
        )
    added = steers.added_details(text, allowed=allowed, names=names)
    if added:
        return GuidanceParsed(
            outcome="rejected", failure="added_details", detail="added: " + ", ".join(added)
        )
    if text == current.strip():
        return GuidanceParsed(outcome="unchanged")
    return GuidanceParsed(outcome="updated", text=text)


async def write_guidance(
    sessionmaker: async_sessionmaker[AsyncSession],
    model: CheckpointedModel,
    *,
    world_id: UUID,
    steer_id: UUID,
    cassette_thread: str,
    attempt: int = 1,
    model_override: str | None = None,
) -> GuidanceParsed:
    """Ask for the guidance `steer_id` calls for, checkpointed on
    (`keys.steer_guidance_turn_id(steer_id)`, `GUIDANCE_STEP`), and check it. Writes
    nothing: the caller stores the outcome."""
    async with sessionmaker() as session:
        parties = await _parties(session, await _steer(session, world_id, steer_id))
        inputs = await _guidance_inputs(session, parties)
    key = parties.key(
        cassette_thread,
        turn_id=keys.steer_guidance_turn_id(steer_id),
        step=GUIDANCE_STEP,
        attempt=attempt,
    )
    request = guidance_request(
        key, tier=parties.tier, prompt=inputs.prompt, model_override=model_override
    )
    message = (await model.call(request)).message()
    return parse_guidance(
        message, current=inputs.current, allowed=inputs.allowed, names=inputs.names
    )


# --- The held rewrite -----------------------------------------------------------------


async def rewrite_inputs(session: AsyncSession, steer: Steer) -> str:
    """The rewrite prompt for `steer`'s held item: the thread, the current guidance,
    the held text (the member's wording when they changed it) and the instruction."""
    return await _rewrite_prompt(session, await _parties(session, steer))


async def _rewrite_prompt(session: AsyncSession, parties: _Parties) -> str:
    steer = parties.steer
    if steer.escalation_id is None:
        raise ValueError(f"steer {steer.id} has no item to rewrite")
    org_name, counterparty_name = parties.names()
    held = steer.draft_text
    if held is None or not held.strip():
        item = await repo.get_required(session, Escalation, steer.world_id, steer.escalation_id)
        draft = await escalations.current_draft(session, item)
        held = draft.answer if draft is not None else ""
    return render(
        "held_rewrite",
        {
            "org_name": org_name,
            "counterparty_name": counterparty_name,
            "side_line": parties.side_line(),
            "member_first": first_name(parties.author.name) or "Your teammate",
            "thread_quoted": await _thread_text(session, parties),
            "guidance": await steers.current_guidance_quoted(
                session,
                steer.world_id,
                steer.thread_id,
                steer.side,
                org_name=org_name,
                empty=NO_GUIDANCE,
            ),
            "message": context.quoted("message", held, from_=org_name),
            "instruction": parties.instruction(),
        },
    )


def rewrite_request(
    key: CallKey, *, tier: Tier, prompt: str, model_override: str | None = None
) -> ModelRequest:
    """The rewrite call: the prompt alone, no tools, the message as the output."""
    return single_call(
        key,
        tier=tier,
        prompt=prompt,
        max_tokens=STEER_MAX_TOKENS,
        output_schema=HELD_REWRITE_SCHEMA,
        model_override=model_override,
    )


async def rewrite_held(
    sessionmaker: async_sessionmaker[AsyncSession],
    model: CheckpointedModel,
    *,
    world_id: UUID,
    steer_id: UUID,
    cassette_thread: str,
    attempt: int = 1,
    model_override: str | None = None,
) -> str | None:
    """The held message rewritten as `steer_id` asks, checkpointed on
    (`keys.steer_rewrite_turn_id(steer_id)`, `REWRITE_STEP`); None when the call
    refused or returned nothing usable. Writes nothing."""
    async with sessionmaker() as session:
        parties = await _parties(session, await _steer(session, world_id, steer_id))
        prompt = await _rewrite_prompt(session, parties)
    key = parties.key(
        cassette_thread,
        turn_id=keys.steer_rewrite_turn_id(steer_id),
        step=REWRITE_STEP,
        attempt=attempt,
    )
    request = rewrite_request(key, tier=parties.tier, prompt=prompt, model_override=model_override)
    message = (await model.call(request)).message()
    if failed_output(message, who="the rewrite") is not None:
        return None
    try:
        rewritten = _RewriteOutput.model_validate_json(output_text(message)).message.strip()
    except ValidationError:
        return None
    return rewritten or None


# --- Reading ----------------------------------------------------------------------------


async def _steer(session: AsyncSession, world_id: UUID, steer_id: UUID) -> Steer:
    return await repo.get_required(session, Steer, world_id, steer_id)


async def _parties(session: AsyncSession, steer: Steer) -> _Parties:
    world_id = steer.world_id
    thread = await repo.get_required(session, Thread, world_id, steer.thread_id)
    counterparty_id = threads.org_of(thread, threads.other(steer.side))
    scenario = await session.scalar(select(World.scenario).where(World.id == world_id))
    return _Parties(
        scenario=scenario or "",
        steer=steer,
        thread=thread,
        org=await repo.get_required(session, Org, world_id, steer.org_id),
        counterparty=await repo.get_required(session, Org, world_id, counterparty_id),
        author=await repo.get_required(session, Member, world_id, steer.author_member_id),
    )


async def _thread_text(session: AsyncSession, parties: _Parties) -> str:
    """The thread's messages: the side's own quoted, the counterparty's inside their
    delimiter with forged notices marked, as the representative reads them."""
    org_name, counterparty_name = parties.names()
    messages = await session.scalars(
        repo.in_world(ThreadMessage, parties.thread.world_id)
        .where(ThreadMessage.thread_id == parties.thread.id, ThreadMessage.kind == "message")
        .order_by(ThreadMessage.seq)
    )
    blocks = [
        context.quoted("message", message.body, from_=org_name)
        if message.from_org_id == parties.org.id
        else context.wrap_counterparty(message.body, counterparty_name=counterparty_name)
        for message in messages
    ]
    return "\n\n".join(blocks) or "No messages yet."


__all__ = [
    "GUIDANCE_STEP",
    "HELD_REWRITE_SCHEMA",
    "NO_GUIDANCE",
    "REOPENING",
    "REWRITE_STEP",
    "STEER_GUIDANCE_SCHEMA",
    "STEER_MAX_TOKENS",
    "WAITING",
    "GuidanceInputs",
    "GuidanceOutcome",
    "GuidanceParsed",
    "guidance_inputs",
    "guidance_request",
    "parse_guidance",
    "rewrite_held",
    "rewrite_inputs",
    "rewrite_request",
    "write_guidance",
]
