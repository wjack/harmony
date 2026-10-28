"""The consult answerer, the sharing check, and settling a consult
(REVISION_UNSTRUCTURED.md §4, §6).

1. `answer`: internal Claude answers for its org (R§4.1). It runs a short tool loop over
   `ANSWERER_TOOLS` (the org's records as they are now, through `RecordReader`), from
   prompts/consult.md, and ends in the structured draft (`CONSULT_OUTPUT_SCHEMA`,
   validated as `ConsultDraft`). The prompt says how the answer will be released under
   the mode toward this counterparty, and on the requester side who sent the request,
   its brief and what it asks this counterparty (org context the member approved, R§7).
   A refusal, an invalid draft or
   a loop that runs out of steps is `AnswerFailed`: the consult goes to the default
   assignee with no draft.
2. `check_sharing`: the sharing check (R§4.3), which the caller runs only when
   `release.needs_sharing_check`. A separate call from prompts/sharing_check.md that
   sees the sharing descriptions for this counterparty, the question and the draft
   answer, never the org's records. A check that can't run counts as failed.
3. `settle`: records the draft (and the verdict the decision used) on the consult,
   applies `release.decide` under the mode read live, and releases the answer
   (`consults.release`, by `auto`) or opens the consult's escalation with the draft and
   routes it by the answerer's proposal (`routing.apply_route`). A redraft only adds a
   draft rev to the open escalation: a person decides it.
4. One answer, many askers. `match_question` asks Claude (`same_question.md`, one call
   under the `screener` role) whether a requester-side question asks one of the
   request's cards again while it waits for its answer. A rev the `use_answer` Update
   reserves starts from the member's answer to another item on its card (`Reuse`). A
   later asker never does: its question reaches a person afresh. Its prompt keeps the
   first draft's review text and adds
   `consult_reuse.md` in place of a redraft's: the member's answer and its date, with
   other organizations' names replaced, never the question it answered. The sharing
   check runs on it in every mode, and `settle` may release it by `auto` even on an
   open escalation, which then closes with no member (`escalations.resolve_released`):
   it was never a member's approval of this rev. Held, the rev carries its own
   `held_reason`.

Every request is built by `llm.requests.build_request`, roles `answerer`,
`sharing_check` and `screener` (the matcher); cassette keys and checkpoints are in
`agents.keys`.

Owner: R-agents (the model classes and output schemas are the contract's).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Final, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.agents import context, keys, release
from harmony.agents.models import EscalationAction, Route
from harmony.agents.prompts import render
from harmony.agents.runtime import (
    CheckpointedModel,
    echo_content,
    tool_calls,
    tool_result_block,
)
from harmony.agents.tools.internal import ANSWERER_TOOLS, RecordReader
from harmony.db import repo
from harmony.db.models import (
    Consult,
    Escalation,
    Member,
    NetworkRequest,
    Org,
    RepPolicy,
    Thread,
    ThreadMessage,
    TrustedOrganization,
    World,
)
from harmony.db.models.choices import Autonomy
from harmony.db.uow import UnitOfWork
from harmony.domain import clock, consults, escalations, routing, steers, threads
from harmony.domain.settings import skips_approvals
from harmony.llm import CallKey, ModelRequest
from harmony.llm.requests import Tier, build_request
from harmony.llm.wire import JsonObject, as_object, as_str, objects
from harmony.names import first_name

log = logging.getLogger(__name__)

# Adaptive thinking shares this with the JSON.
ANSWER_MAX_TOKENS: Final = 16_000
SHARING_CHECK_MAX_TOKENS: Final = 4_000
MATCH_MAX_TOKENS: Final = 4_000
# The most cards one matcher call compares a question with (the newest).
MATCH_MAX_EARLIER: Final = 10
# The `consult` result when a member answered the counterparty themselves.
TAKEN_OVER: Final = "A member of your side wrote to the counterparty directly."
# The `send_message` result when a member declined the held message and gave no reason.
HELD_DECLINED: Final = "Not sent: your side didn't approve it."
# The verdict stored when the mode became *Auto* after the caller decided no check was
# needed: the draft goes to a person rather than out unchecked.
UNCHECKED: Final = "The sharing check didn't run on this answer, so a teammate reviews it."


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConsultDraft(_Model):
    """The answerer's output (prompts/consult.md "What to return"). What it looked at
    is not part of the draft: it is recorded from its tool calls."""

    # Text for the representative.
    answer: str
    # Set for anything that commits the org, unless its instructions plainly allow it
    # (§4.6; prompt policy, not code).
    needs_human: bool
    needs_human_reason: str | None
    # Used only when a person is needed (§6).
    route: Route | None
    rationale: str
    # The decision in one line, for the teammate who reviews it; drafts from before
    # it was asked for have none.
    short_question: str | None = None


class SharingCheck(_Model):
    """The sharing check's verdict on a draft answer (§4.3), stored as
    `consults.sharing_check`. It sees only the applicable sharing description(s), the
    question and the draft, never the org's records."""

    within_sharing: bool
    reason: str


class DraftReady(_Model):
    kind: Literal["draft"] = "draft"
    draft: ConsultDraft
    # The answerer's turn (`consults.answer_turn_id`): its `model_calls` hold the tool
    # calls the reviewer sees. `step_idx` is the step that wrote the draft.
    turn_id: UUID
    step_idx: int


class AnswerFailed(_Model):
    """No usable draft: route to the default assignee with none (§5)."""

    kind: Literal["failed"] = "failed"
    reason: Literal["refusal", "invalid"]
    detail: str


ConsultAnswer = Annotated[DraftReady | AnswerFailed, Field(discriminator="kind")]


class Reuse(_Model):
    """A member's answer to another item on the consult's card, which a draft starts
    from: the item it settled, the words, who wrote them and on which world date."""

    escalation_id: UUID
    answer: str
    member_name: str
    answered_on: date


class _Match(_Model):
    same_as: int | None


# --- The answerer --------------------------------------------------------------------


async def answer(
    sessionmaker: async_sessionmaker[AsyncSession],
    model: CheckpointedModel,
    *,
    world_id: UUID,
    consult_id: UUID,
    draft_rev: int = 1,
    instruction: str | None = None,
    base_text: str | None = None,
    member_name: str | None = None,
    reuse: Reuse | None = None,
    attempt: int = 1,
    model_override: str | None = None,
    cassette_thread: str | None = None,
    consult_turn: int | None = None,
) -> DraftReady | AnswerFailed:
    """Draft (or redraft, `draft_rev` > 1) the answer to a consult: a tool loop of at
    most `keys.ANSWERER_STEPS` calls, each checkpointed on
    (`keys.consult_turn_id(consult_id)`, `keys.answerer_step(draft_rev, step)`), so a
    retried activity replays its stored calls.

    A redraft's prompt adds the draft it revises (`base_text`, the member's unsaved
    wording, when given) and `instruction`, what the member (`member_name`) asked for
    (`answer_inputs`); a draft from a member's earlier answer (`reuse`), at any rev,
    adds that answer instead.
    `cassette_thread` and `consult_turn` (the workflow's fixed keys) default to
    `threads.cassette_key` and `consults.ordinal`.
    """
    if draft_rev < 1:
        raise ValueError(f"draft_rev must be >= 1, got {draft_rev}")
    async with sessionmaker() as session:
        consult = await consults.get(session, world_id=world_id, consult_id=consult_id)
        inputs = await answer_inputs(
            session,
            consult,
            draft_rev=draft_rev,
            instruction=instruction,
            base_text=base_text,
            member_name=member_name,
            reuse=reuse,
        )
        thread_key, turn = await _cassette(session, consult, cassette_thread, consult_turn)
    reader = RecordReader(sessionmaker, world_id=world_id, org_id=consult.org_id)
    messages: list[JsonObject] = [{"role": "user", "content": inputs.prompt}]
    for step in range(keys.ANSWERER_STEPS):
        key = CallKey(
            world_id=world_id,
            scenario=inputs.scenario,
            org=inputs.org_slug,
            role="answerer",
            thread=thread_key,
            turn=turn,
            turn_id=keys.consult_turn_id(consult_id),
            step_idx=keys.answerer_step(draft_rev, step),
            attempt=attempt,
        )
        request = answer_request(
            key, tier=inputs.tier, messages=messages, model_override=model_override
        )
        call = await model.call(request)
        message = call.message()
        if message.get("stop_reason") != "tool_use":
            return parse_answer(message, turn_id=call.turn_id, step_idx=call.step_idx)
        results: list[JsonValue] = []
        for tool_call in tool_calls(message):
            read = await reader.execute(tool_call)
            results.append(
                tool_result_block(tool_call.id, read.result or "", is_error=read.is_error)
            )
        messages.append({"role": "assistant", "content": list[JsonValue](echo_content(message))})
        messages.append({"role": "user", "content": results})
    return AnswerFailed(
        reason="invalid", detail=f"no draft after {keys.ANSWERER_STEPS} steps of looking things up"
    )


def answer_request(
    key: CallKey,
    *,
    tier: Tier,
    messages: Sequence[JsonObject],
    model_override: str | None = None,
) -> ModelRequest:
    """One answerer call: the rendered prompt as the first user message, the read
    tools, and the structured draft as the output format."""
    return build_request(
        key,
        tier=tier,
        system=None,
        messages=messages,
        max_tokens=ANSWER_MAX_TOKENS,
        tools=ANSWERER_TOOLS,
        output_schema=CONSULT_OUTPUT_SCHEMA,
        model_override=model_override,
    )


def parse_answer(
    message: Mapping[str, Any], *, turn_id: UUID, step_idx: int
) -> DraftReady | AnswerFailed:
    """The draft in the answerer's final message. A refusal, a truncated output or one
    that doesn't validate is `AnswerFailed`."""
    failed = failed_output(message, who="the answerer")
    if failed is not None:
        return failed
    try:
        draft = ConsultDraft.model_validate_json(output_text(message))
    except ValidationError as error:
        return AnswerFailed(reason="invalid", detail=first_error(error))
    draft = draft.model_copy(update={"answer": draft.answer.strip()})
    return DraftReady(draft=draft, turn_id=turn_id, step_idx=step_idx)


# --- The sharing check ---------------------------------------------------------------


async def check_sharing(
    sessionmaker: async_sessionmaker[AsyncSession],
    model: CheckpointedModel,
    *,
    world_id: UUID,
    consult_id: UUID,
    draft: ConsultDraft,
    draft_rev: int = 1,
    reused: bool = False,
    attempt: int = 1,
    model_override: str | None = None,
    cassette_thread: str | None = None,
    consult_turn: int | None = None,
) -> SharingCheck:
    """Whether everything `draft` reveals is within what the org shares with this
    counterparty, and, for a draft from a member's earlier answer (`reused`), that it
    neither names nor hints at another organization. Checkpointed on
    (`keys.sharing_turn_id(consult_id)`, `draft_rev - 1`). A refusal or an invalid
    verdict counts as a failed check, with the reason saying so."""
    if draft_rev < 1:
        raise ValueError(f"draft_rev must be >= 1, got {draft_rev}")
    async with sessionmaker() as session:
        consult = await consults.get(session, world_id=world_id, consult_id=consult_id)
        parties = await _parties(session, consult)
        thread_key, turn = await _cassette(session, consult, cassette_thread, consult_turn)
    org_name, counterparty_name = parties.names()
    prompt = render(
        "sharing_check",
        {
            "org_name": org_name,
            "counterparty_name": counterparty_name,
            "sharing": _sharing_text(parties),
            "question": context.quoted("question", consult.question),
            "answer": context.quoted("answer", draft.answer),
            "reuse": REUSED_CHECK if reused else "",
        },
    )
    key = CallKey(
        world_id=world_id,
        scenario=parties.scenario,
        org=parties.org.slug,
        role="sharing_check",
        thread=thread_key,
        turn=turn,
        turn_id=keys.sharing_turn_id(consult_id),
        step_idx=draft_rev - 1,
        attempt=attempt,
    )
    request = single_call(
        key,
        tier=parties.tier,
        prompt=prompt,
        max_tokens=SHARING_CHECK_MAX_TOKENS,
        output_schema=SHARING_CHECK_OUTPUT_SCHEMA,
        model_override=model_override,
    )
    message = (await model.call(request)).message()
    failed = failed_output(message, who="the sharing check")
    if failed is None:
        try:
            check = SharingCheck.model_validate_json(output_text(message))
        except ValidationError as error:
            failed = AnswerFailed(reason="invalid", detail=first_error(error))
        else:
            return check.model_copy(update={"reason": check.reason.strip()})
    return failed_check(failed.detail)


# What the sharing check adds for a draft from a member's earlier answer.
REUSED_CHECK: Final = (
    " This answer was drafted from what a teammate told another conversation, so it also "
    "fails if it names or hints at another organization, or says that anyone else asked "
    "or was told."
)


def failed_check(detail: str) -> SharingCheck:
    """The verdict on a draft the sharing check couldn't judge: failed, so a person
    reviews it."""
    return SharingCheck(within_sharing=False, reason=f"The sharing check couldn't run ({detail}).")


# --- The prompt ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnswerInputs:
    scenario: str
    org_slug: str
    tier: Tier
    prompt: str


@dataclass(frozen=True, slots=True)
class _Parties:
    """The consulting org, its counterparty on the thread, and the policy toward it,
    read now (policy is live, R§3)."""

    scenario: str
    thread: Thread
    org: Org
    counterparty: Org
    policy: RepPolicy | None
    trusted: TrustedOrganization | None
    # The world's Skip approvals (`release.decide`'s `skip_people`), read now.
    skip_people: bool

    @property
    def tier(self) -> Tier:
        return "featured" if self.org.featured else "background"

    def names(self) -> tuple[str, str]:
        return " ".join(self.org.name.split()), " ".join(self.counterparty.name.split())

    def mode(self) -> Autonomy:
        """The mode toward this counterparty. With no policy row, a person decides."""
        return release.mode_for(self.policy, self.trusted) if self.policy else "ask"


async def answer_inputs(
    session: AsyncSession,
    consult: Consult,
    *,
    draft_rev: int = 1,
    instruction: str | None = None,
    base_text: str | None = None,
    member_name: str | None = None,
    reuse: Reuse | None = None,
) -> AnswerInputs:
    """The answerer's prompt, read now: settings, policy and the side's guidance are
    live. A redraft (`draft_rev` > 1) is a person's to release, and its prompt ends with
    the draft that person reviewed (or their wording of it), why it needed them and
    what they asked for. A draft from a member's earlier answer (`reuse`) is released
    as a first draft is, at any rev: its prompt keeps the first draft's review text and
    ends with that answer instead (`_reuse_text`)."""
    parties = await _parties(session, consult)
    org = parties.org
    org_name, counterparty_name = parties.names()
    members = sorted(
        await repo.list_in_world(session, Member, consult.world_id, Member.org_id == org.id),
        key=lambda member: member.slug,
    )
    settings = await context.current_settings(session, org)
    values = {
        "org_name": org_name,
        "counterparty_name": counterparty_name,
        # The prompt is rendered for each consult, so the date never stales a cache.
        "today": context.world_date_notice(await clock.today(session, consult.world_id)),
        "sharing": _sharing_text(parties),
        "review": _review_text(parties, redraft=draft_rev > 1 and reuse is None),
        "instructions": settings.instructions.strip() or "None.",
        "member_profiles": _members_text(members),
        "routing_prompt": _routing_text(parties.policy, members),
        "guidance": await steers.current_guidance_quoted(
            session, consult.world_id, consult.thread_id, consult.side, org_name=org_name
        ),
        "request": await _request_text(session, parties, consult, members),
        "thread_quoted": await _thread_text(session, parties),
        "earlier": await _earlier_text(session, consult),
        "question": context.quoted("question", consult.question),
    }
    prompt = render("consult", values)
    if reuse is not None:
        prompt += "\n\n" + await _reuse_text(session, parties, reuse)
    elif draft_rev > 1:
        prompt += "\n\n" + await _redraft_text(
            session, consult, org_name, instruction, base_text=base_text, member_name=member_name
        )
    return AnswerInputs(
        scenario=parties.scenario, org_slug=org.slug, tier=parties.tier, prompt=prompt
    )


async def _parties(session: AsyncSession, consult: Consult) -> _Parties:
    world_id = consult.world_id
    thread = await repo.get_required(session, Thread, world_id, consult.thread_id)
    counterparty_id = threads.org_of(thread, threads.other(consult.side))
    scenario = await session.scalar(select(World.scenario).where(World.id == world_id))
    return _Parties(
        scenario=scenario or "",
        thread=thread,
        org=await repo.get_required(session, Org, world_id, consult.org_id),
        counterparty=await repo.get_required(session, Org, world_id, counterparty_id),
        policy=await session.get(RepPolicy, (world_id, consult.org_id)),
        trusted=await session.get(TrustedOrganization, (world_id, consult.org_id, counterparty_id)),
        skip_people=await skips_approvals(session, world_id),
    )


async def _cassette(
    session: AsyncSession, consult: Consult, thread_key: str | None, turn: int | None
) -> tuple[str, int]:
    """The (thread, turn) of the consult's cassette keys."""
    if thread_key is None:
        thread = await repo.get_required(session, Thread, consult.world_id, consult.thread_id)
        thread_key = await threads.cassette_key(session, thread)
    return thread_key, turn or await consults.ordinal(session, consult)


def _sharing_text(parties: _Parties) -> str:
    """The sharing descriptions for this counterparty, each quoted in its own part."""
    org_name, counterparty_name = parties.names()
    parts = context.sharing_parts(
        parties.policy, parties.trusted, counterparty_name=counterparty_name
    )
    if not parts:
        return f"{org_name} hasn't described anything its representative may pass on."
    return context.sharing_text(parts, counterparty_name=counterparty_name)


# How an answer reaches the representative under each mode (R§4.4), so the answerer
# knows whether anything stands between its draft and the counterparty.
_REVIEW: Final[Mapping[Autonomy, str]] = {
    "ask": (
        "A teammate reviews every answer before it's released. Draft the answer the "
        "representative should give, even when it goes beyond this description."
    ),
    "auto": (
        "Every answer is checked against this description before it's released, and a "
        "teammate reviews anything outside it. Draft the answer the representative should "
        "give, even when it goes beyond the description."
    ),
    "skip": (
        "Nothing checks an answer before it's released: unless you set `needs_human`, the "
        "representative gets your answer as you write it. Draft the answer it should "
        "give, and set `needs_human` when that goes beyond this description."
    ),
}
# Under Skip approvals, whatever the mode: nobody reviews, and the check declines.
_SKIP_PEOPLE_REVIEW: Final = (
    "Nobody on your side reviews answers right now. Every answer is checked against this "
    "description: one that fits goes to the representative as you write it, even when you "
    "set `needs_human`, and one that doesn't is declined. Draft the answer it should give, "
    "within the description."
)
_REDRAFT_REVIEW: Final = (
    "A teammate asked for this redraft and reviews it: nothing reaches the representative "
    "until they release it. Draft the answer the representative should give, even when it "
    "goes beyond this description."
)


def _review_text(parties: _Parties, *, redraft: bool) -> str:
    """How the answer will be released, for the answerer's prompt. A redraft is a
    teammate's, even under Skip approvals: they asked for it."""
    if redraft:
        return _REDRAFT_REVIEW
    return _SKIP_PEOPLE_REVIEW if parties.skip_people else _REVIEW[parties.mode()]


async def _request_text(
    session: AsyncSession, parties: _Parties, consult: Consult, members: Sequence[Member]
) -> str:
    """On the requester side, who sent the request, the brief they approved and what it
    asks this counterparty; on the responder side, only that the counterparty came with
    one."""
    org_name, counterparty_name = parties.names()
    if consult.side == "responder":
        return (
            f"{counterparty_name} came to {org_name} with a request; what they need is in "
            "the conversation below."
        )
    request = await repo.get_required(
        session, NetworkRequest, consult.world_id, parties.thread.request_id
    )
    sender = next((m for m in members if m.id == request.created_by_member_id), None)
    who = f"{sender.name} (`{sender.slug}`)" if sender is not None else f"Someone at {org_name}"
    if await context.sent_without_review(session, request):
        sent = (
            f"Claude wrote the request this conversation is part of for {who}, and it went "
            "out without their review (this chat's setting). Its brief:"
        )
    else:
        sent = f"{who} sent the request this conversation is part of, and approved its brief:"
    text = f"{sent}\n\n" + context.quoted("brief", request.brief)
    ask = context.recipient_ask(request, parties.counterparty.id)
    if ask is not None:
        text += f"\n\nWhat it asks {counterparty_name}:\n\n" + context.quoted("ask", ask)
    return text


def _members_text(members: Sequence[Member]) -> str:
    lines: list[str] = []
    for member in members:
        title = f", {member.title}" if member.title else ""
        lines.append(f"- `{member.slug}`: {member.name}{title} ({member.role})")
        if member.profile.strip():
            lines.append(f"  {member.profile.strip()}")
    return "\n".join(lines) or "No members."


def _routing_text(policy: RepPolicy | None, members: Sequence[Member]) -> str:
    guidance = (policy.routing_prompt.strip() if policy else "") or "No routing guidance."
    default = next(
        (m for m in members if policy is not None and m.id == policy.default_assignee_id), None
    )
    if default is None:
        return guidance
    return f"{guidance}\n\nDefault assignee: `{default.slug}` ({default.name})."


async def _earlier_text(session: AsyncSession, consult: Consult) -> str:
    """This side's settled consults on the thread, each as the representative received
    its answer, so a new answer keeps to what a teammate decided."""
    earlier = await session.scalars(
        repo.in_world(Consult, consult.world_id)
        .where(
            Consult.thread_id == consult.thread_id,
            Consult.side == consult.side,
            Consult.id != consult.id,
            Consult.status.in_(("released", "declined")),
        )
        .order_by(Consult.created_at, Consult.id)
    )
    told = [
        f"{context.quoted('question', row.question)}\nIt was told:\n{row.released_text or ''}"
        for row in earlier
    ]
    return "\n\n".join(told) or "Nothing yet."


async def _redraft_text(
    session: AsyncSession,
    consult: Consult,
    org_name: str,
    instruction: str | None,
    *,
    base_text: str | None,
    member_name: str | None,
) -> str:
    """The draft the teammate reviewed: their own wording of it when they changed it
    (`base_text`), else the item's current one. Then why the consult needed them, and
    what they asked for."""
    member = " ".join((member_name or "").split()) or "a teammate"
    asked = (instruction or "").strip()
    escalation = await repo.get(
        session, Escalation, consult.world_id, escalation_id_for(consult.id)
    )
    reviewed = None if escalation is None else await escalations.current_draft(session, escalation)
    if base_text is not None and base_text.strip():
        heading = f"The draft as {member} edited it:"
        draft = context.quoted("draft", base_text)
    else:
        heading = "The draft:"
        draft = (
            "There was no draft: the answerer couldn't write one."
            if reviewed is None
            else context.quoted("draft", reviewed.answer)
        )
    return render(
        "consult_redraft",
        {
            "org_name": org_name,
            "member_first": first_name(member_name) or "the teammate",
            "why": release.why_needed(consult, reviewed.held_reason if reviewed else None),
            "draft_heading": heading,
            "draft": draft,
            "instruction": context.quoted("instruction", asked, from_=member)
            if asked
            else "They didn't say what to change.",
        },
    )


async def _reuse_text(session: AsyncSession, parties: _Parties, reuse: Reuse) -> str:
    """The member's answer as this draft may start from it (`consult_reuse.md`): dated,
    with every other organization's name replaced, and without the question it
    answered."""
    org_name, counterparty_name = parties.names()
    orgs = await repo.list_in_world(session, Org, parties.org.world_id)
    others = [org.name for org in orgs if org.id not in (parties.org.id, parties.counterparty.id)]
    answer = context.without_org_names(
        reuse.answer, others, keep=(parties.org.name, parties.counterparty.name)
    )
    member = " ".join(reuse.member_name.split()) or "A teammate"
    return render(
        "consult_reuse",
        {
            "member": member,
            "org_name": org_name,
            "counterparty_name": counterparty_name,
            "date": context.long_date(reuse.answered_on),
            "answer": context.quoted("answer", answer, from_=member),
        },
    )


async def reuse_of(session: AsyncSession, world_id: UUID, escalation_id: UUID) -> Reuse | None:
    """The answer a member approved on the item `escalation_id`, as a draft may start
    from it; None unless a member approved it with words to reuse."""
    escalation = await repo.get(session, Escalation, world_id, escalation_id)
    if escalation is None or escalation.resolved_by_member_id is None:
        return None
    if consults.member_answer([escalation], escalation.resolved_by_member_id) is None:
        return None
    words = await escalations.settled_words(session, escalation)
    if not words:
        return None
    member = await repo.get_required(session, Member, world_id, escalation.resolved_by_member_id)
    return Reuse(
        escalation_id=escalation.id,
        answer=words,
        member_name=member.name,
        answered_on=await clock.today(session, world_id, escalation.resolved_at),
    )


async def match_question(
    sessionmaker: async_sessionmaker[AsyncSession],
    model: CheckpointedModel,
    *,
    world_id: UUID,
    consult_id: UUID,
    attempt: int = 1,
    model_override: str | None = None,
    cassette_thread: str | None = None,
    consult_turn: int | None = None,
) -> UUID | None:
    """The card whose question this consult asks again, by Claude's judgment, or None:
    one call (`same_question.md`, role `screener`) that sees this question and up to
    `MATCH_MAX_EARLIER` cards' questions, each quoted with the organization its
    conversation is with, and returns a number. No call when no card could be joined
    (`consults.cards_to_join`). Checkpointed on (`keys.match_turn_id(consult_id)`, 0).
    A refusal or an unreadable answer is no match."""
    async with sessionmaker() as session:
        consult = await consults.get(session, world_id=world_id, consult_id=consult_id)
        parties = await _parties(session, consult)
        cards = await consults.cards_to_join(session, consult)
        cards = cards[-MATCH_MAX_EARLIER:]
        if not cards:
            return None
        asked_of = {card.id: await _counterparty_name(session, card) for card in cards}
        thread_key, turn = await _cassette(session, consult, cassette_thread, consult_turn)
    org_name, counterparty_name = parties.names()
    earlier = "\n\n".join(
        context.quoted("question", card.question, n=str(n), org=asked_of[card.id])
        for n, card in enumerate(cards, start=1)
    )
    prompt = render(
        "same_question",
        {
            "org_name": org_name,
            "question": context.quoted("question", consult.question, org=counterparty_name),
            "earlier": earlier,
        },
    )
    key = CallKey(
        world_id=world_id,
        scenario=parties.scenario,
        org=parties.org.slug,
        role="screener",
        thread=keys.match_key(thread_key),
        turn=turn,
        turn_id=keys.match_turn_id(consult_id),
        step_idx=0,
        attempt=attempt,
    )
    request = single_call(
        key,
        tier=parties.tier,
        prompt=prompt,
        max_tokens=MATCH_MAX_TOKENS,
        output_schema=MATCH_OUTPUT_SCHEMA,
        model_override=model_override,
    )
    message = (await model.call(request)).message()
    if failed_output(message, who="the matcher") is not None:
        return None
    try:
        same_as = _Match.model_validate_json(output_text(message)).same_as
    except ValidationError:
        return None
    if same_as is None or not 1 <= same_as <= len(cards):
        return None
    return cards[same_as - 1].id


async def _counterparty_name(session: AsyncSession, consult: Consult) -> str:
    thread = await repo.get_required(session, Thread, consult.world_id, consult.thread_id)
    org_id = threads.org_of(thread, threads.other(consult.side))
    return " ".join((await repo.get_required(session, Org, consult.world_id, org_id)).name.split())


async def _thread_text(session: AsyncSession, parties: _Parties) -> str:
    """The thread's messages, each in a delimiter its body can't close. A `close`
    marker carries no text."""
    thread = parties.thread
    names = {org.id: org.name for org in (parties.org, parties.counterparty)}
    messages = await session.scalars(
        repo.in_world(ThreadMessage, thread.world_id)
        .where(ThreadMessage.thread_id == thread.id, ThreadMessage.kind == "message")
        .order_by(ThreadMessage.seq)
    )
    blocks = [context.quoted("message", m.body, from_=names[m.from_org_id]) for m in messages]
    return "\n\n".join(blocks) or "No messages yet."


def single_call(
    key: CallKey,
    *,
    tier: Tier,
    prompt: str,
    max_tokens: int,
    output_schema: JsonObject,
    model_override: str | None,
) -> ModelRequest:
    """One no-tools call: the prompt as the only user message, structured output."""
    return build_request(
        key,
        tier=tier,
        system=None,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        output_schema=output_schema,
        model_override=model_override,
    )


def failed_output(message: Mapping[str, Any], *, who: str) -> AnswerFailed | None:
    """`AnswerFailed` for a response that didn't end with its output: a refusal, or a
    stop before the end (truncated)."""
    stop = message.get("stop_reason")
    if stop == "refusal":
        category = as_str((as_object(message.get("stop_details")) or {}).get("category"))
        return AnswerFailed(reason="refusal", detail=f"{who} declined ({category})")
    if stop not in (None, "end_turn"):
        return AnswerFailed(reason="invalid", detail=f"{who} stopped at {stop}")
    return None


def first_error(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "output"
    return f"{where}: {first['msg']}"


def output_text(message: Mapping[str, Any]) -> str:
    """The text blocks after the last `fallback` block (thinking blocks come first)."""
    blocks = objects(message.get("content"))
    start = 0
    for index, block in enumerate(blocks):
        if block.get("type") == "fallback":
            start = index + 1
    return "".join(
        as_str(block.get("text")) or "" for block in blocks[start:] if block.get("type") == "text"
    )


# --- Settling a consult --------------------------------------------------------------


class Answered(_Model):
    """The consult closed without a person: code released the answer under the mode
    (§4.4). `result_text` is the `consult` tool's result (`consults.released_text`)."""

    kind: Literal["answered"] = "answered"
    consult_id: UUID
    result_text: str


class Escalated(_Model):
    """A person decides: an open `consult` escalation, routed."""

    kind: Literal["escalated"] = "escalated"
    consult_id: UUID
    escalation_id: UUID
    draft_rev: int | None
    assignee_ids: list[UUID]
    # Why it needs a person (the release decision or the failed answer).
    reason: str


ConsultSettled = Annotated[Answered | Escalated, Field(discriminator="kind")]


def escalation_id_for(consult_id: UUID) -> UUID:
    """A consult's one escalation (redrafts add drafts to it)."""
    return uuid5(consult_id, "escalation")


async def settle(
    uow: UnitOfWork,
    *,
    consult_id: UUID,
    outcome: DraftReady | AnswerFailed,
    draft_rev: int = 1,
    sharing: SharingCheck | None = None,
    reused_from: UUID | None = None,
) -> Answered | Escalated:
    """Release the answer or hand it to a person (module docstring). `sharing` is the
    check's verdict when the caller ran it; `reused_from`, the item whose member answer
    a rev of an open escalation started from. Idempotent: a settled consult returns what
    it settled to, and a retry or redraft of an escalated one only adds its draft rev
    (while the escalation is open), unless the rev was drafted from a member's earlier
    answer (`_settle_reused`). IDs derive from the consult."""
    session = uow.session
    consult = await consults.get(session, world_id=uow.world_id, consult_id=consult_id)
    settled = settled_result(consult)
    if settled is not None:
        return Answered(consult_id=consult_id, result_text=settled)
    escalation = await repo.get(session, Escalation, uow.world_id, escalation_id_for(consult_id))
    if escalation is not None:
        if (
            reused_from is not None
            and isinstance(outcome, DraftReady)
            and escalation.status == "open"
        ):
            return await _settle_reused(
                uow, consult, escalation, outcome, draft_rev, sharing, reused_from
            )
        added = False
        if isinstance(outcome, DraftReady) and escalation.status == "open":
            await escalations.add_draft(
                uow, escalation.id, outcome.draft, turn_id=outcome.turn_id, draft_rev=draft_rev
            )
            added = True
        current = await escalations.current_draft(session, escalation)
        return Escalated(
            consult_id=consult_id,
            escalation_id=escalation.id,
            draft_rev=draft_rev if added else None,
            assignee_ids=escalations.assignee_ids(escalation),
            reason=release.why_needed(consult, current.held_reason if current else None),
        )
    parties = await _parties(session, consult)
    if isinstance(outcome, AnswerFailed):
        log.info("consult %s has no draft: %s: %s", consult_id, outcome.reason, outcome.detail)
        consult = await consults.record_draft(
            uow, consult_id, None, answer_turn_id=keys.consult_turn_id(consult_id)
        )
        if parties.skip_people:
            return await _decline(uow, consult_id, outcome.detail)
        return await _escalate(uow, consult, None, draft_rev)

    draft = outcome.draft
    consult = await consults.record_draft(uow, consult_id, draft, answer_turn_id=outcome.turn_id)
    mode, skip_people = parties.mode(), parties.skip_people
    check: SharingCheck | None = None
    if release.needs_sharing_check(mode, draft, skip_people=skip_people):
        check = sharing or SharingCheck(within_sharing=False, reason=UNCHECKED)
        consult = await consults.record_sharing_check(uow, consult_id, check)
    match release.decide(mode, draft, check, skip_people=skip_people):
        case release.Release(answer=text):
            released = result_text("approve", text)
            await consults.release(uow, consult_id, text=released, released_by="auto")
            return Answered(consult_id=consult_id, result_text=released)
        case release.Decline(detail=detail):
            return await _decline(uow, consult_id, detail)
        case release.NeedsHuman():
            return await _escalate(uow, consult, outcome, draft_rev)


async def _decline(uow: UnitOfWork, consult_id: UUID, why: str) -> Answered:
    """Under Skip approvals, a consult nobody may release: declined by code, so the
    representative is told its side declined and no person is asked."""
    log.info("consult %s declined under Skip approvals: %s", consult_id, why)
    declined = result_text("decline", "")
    await consults.decline(uow, consult_id, text=declined, released_by="auto")
    return Answered(consult_id=consult_id, result_text=declined)


async def _settle_reused(
    uow: UnitOfWork,
    consult: Consult,
    escalation: Escalation,
    outcome: DraftReady,
    draft_rev: int,
    sharing: SharingCheck | None,
    reused_from: UUID,
) -> Answered | Escalated:
    """A rev of an open item drafted from a member's answer to another item on its card
    (`use_answer`): decided under the mode and the sharing check toward this
    counterparty, as a first draft is. Released, it goes out by `auto` and the item
    closes with no member; held, the rev carries why (`held_reason`)."""
    draft = outcome.draft
    consult = await consults.record_drafted_from(uow, consult.id, reused_from)
    # The item is already a person's: under Skip approvals too, a rev the check stops
    # waits for them (it was opened before the switch, or it would never have opened).
    mode = (await _parties(uow.session, consult)).mode()
    check: SharingCheck | None = None
    if release.needs_sharing_check(mode, draft, reused=True):
        check = sharing or SharingCheck(within_sharing=False, reason=UNCHECKED)
    decision = release.decide(mode, draft, check, reused=True)
    held = None if isinstance(decision, release.Release) else decision.detail
    await escalations.add_draft(
        uow, escalation.id, draft, turn_id=outcome.turn_id, draft_rev=draft_rev, held_reason=held
    )
    if held is None:
        released = result_text("approve", draft.answer)
        await consults.release(uow, consult.id, text=released, released_by="auto")
        await escalations.resolve_released(uow, escalation.id)
        return Answered(consult_id=consult.id, result_text=released)
    log.info("consult %s rev %d needs a person: %s", consult.id, draft_rev, held)
    return Escalated(
        consult_id=consult.id,
        escalation_id=escalation.id,
        draft_rev=draft_rev,
        assignee_ids=escalations.assignee_ids(escalation),
        reason=held,
    )


async def _escalate(
    uow: UnitOfWork,
    consult: Consult,
    outcome: DraftReady | None,
    draft_rev: int,
) -> Escalated:
    """Open the consult's escalation with the draft, if any, and route it by the
    answerer's proposal (the default assignee without one)."""
    escalation, _ = await escalations.open(
        uow,
        escalation_id=escalation_id_for(consult.id),
        org_id=consult.org_id,
        kind="consult",
        dedupe_key=f"consult:{consult.id}",
        thread_id=consult.thread_id,
        side=consult.side,
        consult_id=consult.id,
    )
    draft = outcome.draft if outcome is not None else None
    if outcome is not None:
        await escalations.add_draft(
            uow, escalation.id, outcome.draft, turn_id=outcome.turn_id, draft_rev=draft_rev
        )
    consult = await consults.mark_needs_human(uow, consult.id)
    routed = await routing.apply_route(uow, escalation, draft.route if draft else None)
    reason = release.why_needed(consult)
    log.info("consult %s needs a person: %s", consult.id, reason)
    return Escalated(
        consult_id=consult.id,
        escalation_id=escalation.id,
        draft_rev=draft_rev if draft is not None else None,
        assignee_ids=routed,
        reason=reason,
    )


def result_text(action: EscalationAction, text: str) -> str:
    """The `consult` tool result the representative receives, however the consult was
    settled (by code or by a person). What a side released is model-drafted or
    member-written, so it is quoted (`context.quote_released`), never Harmony's words."""
    match action:
        case "approve":
            return "Your side answered:\n" + context.quote_released(text)
        case "decline":
            return _with_note("Your side declined to answer.", text)
        case "take_over":
            return _sent_instead(TAKEN_OVER, text)


def held_result(action: EscalationAction, *, text: str, drafted: str, resolved: str) -> str:
    """The `send_message` result once a person settled the held message: what crossed
    when it isn't what the representative wrote (`drafted`, rev 1), quoted like
    `result_text`. An approval without `text` sends the rev the member approved
    (`resolved`), which a steer may have rewritten."""
    match action:
        case "approve":
            sent = text if text.strip() else resolved
            if sent.strip() and sent.strip() != drafted.strip():
                return "Sent as your side edited it:\n" + context.quote_released(sent)
            return "Sent, approved by your side."
        case "decline":
            return _with_note(HELD_DECLINED, text)
        case "take_over":
            return _sent_instead(f"Not sent: {TAKEN_OVER}", text)


def _with_note(said: str, note: str) -> str:
    if not note.strip():
        return said
    return f"{said} What you may say:\n" + context.quote_released(note)


def _sent_instead(said: str, sent: str) -> str:
    if not sent.strip():
        return said
    return f"{said} What they sent:\n" + context.quote_released(sent)


def settled_result(consult: Consult) -> str | None:
    """The `consult` result a settled consult owes (a retried settle), or None while a
    person or the answerer is still on it."""
    if consult.status in ("released", "declined"):
        return consult.released_text or ""
    return None


# --- The output schema -----------------------------------------------------------------

_NULLABLE_STRING: Final[JsonObject] = {"anyOf": [{"type": "string"}, {"type": "null"}]}


# The answerer's structured output. Every property is required (nullable where
# optional), as structured outputs expect; numeric and length limits aren't supported
# there, so `ConsultDraft` enforces them when it validates.
CONSULT_OUTPUT_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "needs_human": {"type": "boolean"},
        "needs_human_reason": _NULLABLE_STRING,
        "route": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "assignees": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "member": {"type": "string", "description": "A member slug."},
                                    "why": {
                                        "type": "string",
                                        "description": "One short clause, addressed to that "
                                        "person: 'you run XRD and SEM'.",
                                    },
                                },
                                "required": ["member", "why"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["assignees"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ]
        },
        "rationale": {"type": "string"},
        "short_question": {
            "type": "string",
            "description": "The decision or question for the teammate who reviews this, "
            "in one line of at most about 15 words.",
        },
    },
    "required": [
        "answer",
        "needs_human",
        "needs_human_reason",
        "route",
        "rationale",
        "short_question",
    ],
    "additionalProperties": False,
}

# The matcher's structured output, validated as `_Match`.
MATCH_OUTPUT_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {"same_as": {"anyOf": [{"type": "integer"}, {"type": "null"}]}},
    "required": ["same_as"],
    "additionalProperties": False,
}

# The sharing check's structured output, validated as `SharingCheck`.
SHARING_CHECK_OUTPUT_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "within_sharing": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["within_sharing", "reason"],
    "additionalProperties": False,
}


__all__ = [
    "ANSWER_MAX_TOKENS",
    "CONSULT_OUTPUT_SCHEMA",
    "HELD_DECLINED",
    "MATCH_MAX_EARLIER",
    "MATCH_MAX_TOKENS",
    "MATCH_OUTPUT_SCHEMA",
    "REUSED_CHECK",
    "SHARING_CHECK_MAX_TOKENS",
    "SHARING_CHECK_OUTPUT_SCHEMA",
    "TAKEN_OVER",
    "UNCHECKED",
    "AnswerFailed",
    "AnswerInputs",
    "Answered",
    "ConsultAnswer",
    "ConsultDraft",
    "ConsultSettled",
    "DraftReady",
    "Escalated",
    "Reuse",
    "SharingCheck",
    "answer",
    "answer_inputs",
    "answer_request",
    "check_sharing",
    "escalation_id_for",
    "failed_check",
    "failed_output",
    "first_error",
    "held_result",
    "match_question",
    "output_text",
    "parse_answer",
    "result_text",
    "reuse_of",
    "settle",
    "settled_result",
    "single_call",
]
