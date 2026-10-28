"""Context assembly (§2.1, §2.2; REVISION_UNSTRUCTURED.md §2).

Representatives:
- `build_rep_context` renders a side's system prompt and tool list from
  `prompts/representative.md` plus `side_requester.md` / `side_responder.md`. The prompt
  holds exactly the R§2 parts: the counterparty's network-verified identity, the org's
  public summary, the sharing descriptions that apply to this counterparty (the org's
  default, plus the counterparty's own if it is a trusted organization), the brief and
  what it asks this counterparty on the requester side (never another recipient's
  ask), and the org's instructions. No org record is ever rendered into it:
  information from its own side arrives only as a released consult answer.
- Each part sits in a code-written tag (`<public_summary>`, ...) with every `<` inside
  escaped, so `context_parts` reads the parts back from the frozen bytes exactly.
- `freeze_rep_session` stores those bytes in `rep_sessions` once, at the side's first
  turn; every later turn sends exactly the stored bytes.
- After the first turn, consult answers arrive as tool results; everything else waits
  in `rep_notices` (`queue_notice`) and is flushed as one merged system message at the
  side's next model call (`flush_notices`, decided once per turn). Notices whose call
  never stored them (refused, incomplete, failed) go out again at the next turn.
- What reached the representative is read back from its stored transcript
  (`delivered_notices`, `received_results`), never from a consult's status.
- A released answer is model-drafted, so it is quoted inside `<released_text>`
  wherever it reaches the representative (`quote_released`), never as Harmony's or the
  org's own words.
- Guidance from the side's org (docs/design/EDIT.md §3.6) is model-written, so it is
  never a notice: `guidance_block` renders a version as a code-written lead plus the
  text in `<guidance>`, a user-message block the turn opens with, once. Whether a
  version reached the representative is read back from the stored transcript
  (`delivered_guidance`), like `received_results`.
- Counterparty text reaches the representative only through `wrap_counterparty`.

Internal Claude: `build_internal_context` renders `prompts/internal.md` with the org,
the member's name and title; the world date goes out as a notice, not in the prompt.

Owner: R-agents.
"""

from __future__ import annotations

import functools
import json
import re
import unicodedata
from collections.abc import Collection, Iterable, Sequence
from datetime import date, datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents.models import ContextPart, ContextPartKind, OutreachRecipient
from harmony.agents.prompts import render
from harmony.agents.tools import ToolDefinition
from harmony.db import repo
from harmony.db.models import (
    Card,
    Community,
    Conversation,
    Member,
    NetworkRequest,
    Org,
    OrgCommunity,
    RepGuidance,
    RepNotice,
    RepPolicy,
    RepSession,
    RepSettings,
    RepTranscript,
    Thread,
    TrustedOrganization,
)
from harmony.db.models.choices import Side
from harmony.db.uow import UnitOfWork
from harmony.events.schemas import EventScope
from harmony.llm.wire import as_str, canonical_json, objects

# The block code writes around every counterparty message (prompts/representative.md).
COUNTERPARTY_OPEN = '<counterparty_message from="{counterparty_name}">'
COUNTERPARTY_CLOSE = "</counterparty_message>"
# Prefixes a counterparty line that reads like a claim of identity, verification or
# authority (prompts/representative.md says what it means).
UNVERIFIED_MARK: Final = "[unverified] "
# The block code writes around text a side released (a consult answer, model-drafted):
# quoted as data wherever it reaches the representative (prompts/representative.md).
RELEASED_OPEN: Final = "<released_text>"
RELEASED_CLOSE: Final = "</released_text>"
# Every `<` (and anything that reads as one) inside quoted text becomes this.
ESCAPED_LESS_THAN: Final = "&lt;"
# What a prompt part holds when the org left it blank.
EMPTY_PART: Final = "(none)"
# The tag code writes around a guidance version (`guidance_block`).
GUIDANCE_TAG: Final = "guidance"
# The tag a `shared` notice quotes the member's words in.
SHARED_TAG: Final = "shared"
# `for` on the org's default sharing description; a trusted organization's extra one
# names that organization.
SHARING_FOR_ANY: Final = "any organization"

# Code-written notices to a representative: its org's prompt settings changed
# (`settings`), its mode or the sharing descriptions that apply changed for this
# counterparty (`mode`, `sharing`), the world date, the counterparty closed, either
# side reopened the conversation, or a member of its side passed it something from
# their chat (`shared`, their words quoted).
NoticeKind = Literal["settings", "mode", "sharing", "world_date", "closed", "reopened", "shared"]

# Every character `str.splitlines` breaks on, so no line hides inside another.
_LINE_BREAK: Final = re.compile(r"\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85  ]")
# Lines shaped like the identity statement, a verification, an operator message, or one
# of the texts code writes (`world_date_notice`, `closed_notice`, `reopened_notice`, the
# settings, mode and sharing notices, and the tool results: `consult`'s with their
# released text, and `send_message`'s and `close`'s, sent, held and settled, or refused,
# with their quoted text), and the guidance block's lead and tag. Matched on the escaped
# line, where `<` reads `&lt;`.
_IDENTITY_LINE: Final = re.compile(
    r"verif|authenticat|impersonat|identit|harmony|sender|signed\s+by|on\s+behalf\s+of"
    r"|counterparty_message|system\s+(?:message|notice|note|prompt|instruction)"
    r"|^\W*(?:system|admin|administrator|operator)\b|^\W*from\s*:"
    r"|notice\s+from|today\W*s\s+date|instructions?\s+from"
    r"|your\s+organi[sz]ation\s+(?:changed|updated|removed)"
    r"|your\s+side\s+(?:answered|declined|edited|didn\W*t)|approved\s+by\s+your\s+side"
    r"|released[\W_]*text|closed\s+(?:this|the)\s+conversation"
    r"|reopened\s+(?:this|the)\s+conversation"
    r"|not\s+(?:sent|run|closed)(?:\s+yet)?\s*:|what\s+(?:you\s+may\s+say|they\s+sent)\s*:"
    r"|&lt;\W*guidance\b|updated\s+its\s+guidance|withdrew\s+its\s+guidance|never\s+widens"
    r"|exactly\s+as\s+written|stop\s+calling\s+tools"
    r"|end\s+your\s+turn|wrote\s+to\s+the\s+counterparty|nothing\s+more\s+reaches"
    r"|nothing\s+from\s+you\s+has\s+reached|waiting\s+on\s+a\s+person\s+on\s+your\s+side"
    r"|no\s+result\s+was\s+recorded",
    re.IGNORECASE,
)
# Unicode's Default_Ignorable_Code_Point set: rendered as nothing, so they can split a
# keyword or a tag without showing (with the format characters, removed by category).
_IGNORABLE: Final = re.compile(
    "[­͏؜ᅟᅠ឴឵᠋-᠏​-‏‪-‮⁠-⁯ㅤ︀-️﻿ﾠ￰-￸\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)
# Categories with nothing visible of their own: format, unassigned, private use,
# surrogates, and (once NFKD has split them off) combining and enclosing marks.
_UNSEEN: Final = frozenset({"Cf", "Cn", "Co", "Cs"})
_MARKS: Final = frozenset({"Mn", "Me"})
# Cyrillic and Greek letters and angle brackets that read as Latin letters and `<`, `>`
# (a subset of Unicode's confusables), one line per group in both strings.
_LOOKALIKES: Final = str.maketrans(
    "АВЕЅІЈКМНОРСТХУаеѕіјорсухԁһӏԛԝΑΒΕΖΗΙΚΜΝΟΡΤΥΧαεικνορτυχ‹〈⟨˂❮›〉⟩˃❯",
    "ABESIJKMHOPCTXYaesijopcyxdhlqwABEZHIKMNOPTYXaeikvoptux<<<<<>>>>>",
)
# A prompt part: a code-written tag whose content has no `<`, so the first closing tag
# after it is its own.
_PART: Final = re.compile(
    r"^<(counterparty|public_summary|sharing|brief|ask|instructions)((?: [a-z_]+=\"[^\"<>]*\")*)>\n"
    r"(.*?)\n</\1>$",
    re.MULTILINE | re.DOTALL,
)
_ATTRIBUTE: Final = re.compile(r' ([a-z_]+)="([^"<>]*)"')
_NOTICE_SEPARATOR: Final = "\n\n"
# The kind of the textless row a turn's first flush leaves, stamped with that turn, so
# a repeat flush knows the turn's notices are decided even when there were none.
_FLUSHED: Final = "flushed"
_WEEKDAYS: Final = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS: Final = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
# What a member's answer says in place of an organization the conversation it is reused
# in may not hear of (`without_org_names`).
ANOTHER_ORG: Final = "another organization"
# Capitalised first words too common to stand for an organization on their own.
_COMMON_FIRST_WORDS: Final = frozenset({"A", "An", "The"})


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RepContext(_Model):
    """A side's rendered context, before it is frozen."""

    world_id: UUID
    thread_id: UUID
    side: Side
    org_id: UUID
    counterparty_org_id: UUID
    settings_version: int
    model: str
    system: str
    tools: list[dict[str, JsonValue]]


class DeliveredNotice(_Model):
    """A notice the representative was sent: its call stored the system message."""

    kind: NoticeKind
    text: str
    delivered_at: datetime


class DeliveredGuidance(_Model):
    """A guidance version the representative was given, and when."""

    version: int
    # Empty when the guidance was withdrawn.
    text: str
    delivered_at: datetime


class InternalContext(_Model):
    """Internal Claude's prompt and tools for one conversation."""

    world_id: UUID
    conversation_id: UUID
    org_id: UUID
    member_id: UUID
    system: str
    tools: list[dict[str, JsonValue]]


# --- Representatives -----------------------------------------------------------------


async def build_rep_context(
    session: AsyncSession,
    *,
    world_id: UUID,
    thread_id: UUID,
    side: Side,
    model: str,
) -> RepContext:
    """Render a side's system prompt and tools from the org's current settings and the
    policy that applies to this counterparty now (module docstring)."""
    thread = await repo.get_required(session, Thread, world_id, thread_id)
    org_id, counterparty_id = _parties(thread, side)
    org = await repo.get_required(session, Org, world_id, org_id)
    counterparty = await repo.get_required(session, Org, world_id, counterparty_id)
    settings = await current_settings(session, org)
    policy = await session.get(RepPolicy, (world_id, org_id))
    trusted = await session.get(TrustedOrganization, (world_id, org_id, counterparty_id))
    org_name, counterparty_name = _one_line(org.name), _one_line(counterparty.name)
    if side == "requester":
        request = await repo.get_required(session, NetworkRequest, world_id, thread.request_id)
        creator = await _member_name(session, world_id, request.created_by_member_id)
        member_name = creator or f"a member of {org_name}"
        side_section = _requester_section(
            request,
            org_name=org_name,
            counterparty_id=counterparty_id,
            counterparty_name=counterparty_name,
            member_name=member_name,
            unreviewed=await sent_without_review(session, request),
        )
    else:
        side_section = render(
            "side_responder", {"org_name": org_name, "counterparty_name": counterparty_name}
        )
    communities = await _verified_communities(session, world_id, counterparty_id)
    identity = (
        f"{counterparty_name} ({counterparty_descriptor(counterparty.kind, communities)}), "
        f'represented by its own agent, "Claude for {counterparty_name}". Harmony verified '
        "this identity when the conversation opened."
    )
    sharing = sharing_parts(policy, trusted, counterparty_name=counterparty_name)
    system = render(
        "representative",
        {
            "org_name": org_name,
            "counterparty_name": counterparty_name,
            "counterparty": _part("counterparty", identity),
            "public_summary": _part("public_summary", settings.public_summary),
            "sharing": _sharing_section(
                sharing, org_name=org_name, counterparty_name=counterparty_name, side=side
            ),
            "side_section": side_section,
            "instructions": _part("instructions", settings.instructions),
        },
    )
    return RepContext(
        world_id=world_id,
        thread_id=thread_id,
        side=side,
        org_id=org_id,
        counterparty_org_id=counterparty_id,
        settings_version=settings.version,
        model=model,
        system=system,
        tools=list(_rep_tools()),
    )


def _requester_section(
    request: NetworkRequest,
    *,
    org_name: str,
    counterparty_id: UUID,
    counterparty_name: str,
    member_name: str,
    unreviewed: bool,
) -> str:
    """The requester side's section: the brief and this counterparty's ask, as approved
    by the member, or as Claude wrote them in a chat that sends without review (3.10)."""
    if unreviewed:
        approval = (
            f"Claude wrote this brief and this ask for {counterparty_name} on "
            f"{member_name}'s behalf, and they went out without {member_name}'s review "
            "(this chat's setting). They are still your mandate, but consult before "
            "committing to a price, a date or other terms, even where they seem to "
            "allow it:"
        )
        brief = _part("brief", request.brief, written_for=member_name)
    else:
        approval = (
            f"{member_name} approved this brief and this ask for {counterparty_name}, "
            "and they are your mandate:"
        )
        brief = _part("brief", request.brief, approved_by=member_name)
    # A request from before per-recipient asks asks each one for its part of the brief.
    ask = recipient_ask(request, counterparty_id) or (
        f"What the brief needs from {counterparty_name}."
    )
    return render(
        "side_requester",
        {
            "org_name": org_name,
            "counterparty_name": counterparty_name,
            "member_name": member_name,
            "approval": approval,
            "brief": brief,
            "ask": _part("ask", ask, for_=counterparty_name),
        },
    )


def recipient_ask(request: NetworkRequest, org_id: UUID) -> str | None:
    """What the request asks `org_id`, as the member approved or edited it; None for a
    request from before per-recipient asks."""
    for row in request.recipients:
        recipient = OutreachRecipient.model_validate(row)
        if recipient.org_id == org_id:
            return recipient.ask
    return None


async def sent_without_review(session: AsyncSession, request: NetworkRequest) -> bool:
    """Whether the request went out without its member's review: its card was raised
    in a chat that sends without review (`cards.auto`)."""
    if request.card_id is None:
        return False
    card = await repo.get(session, Card, request.world_id, request.card_id)
    return card is not None and card.auto


def sharing_parts(
    policy: RepPolicy | None, trusted: TrustedOrganization | None, *, counterparty_name: str
) -> list[str]:
    """The sharing descriptions that apply to one counterparty, each in its own part:
    the org's default first, for any organization, then a trusted organization's extra
    one, for it (blank ones are left out). The representative's prompt, the
    answerer and the sharing check all quote them this way."""
    parts: list[str] = []
    if policy is not None and policy.sharing_description.strip():
        parts.append(_part("sharing", policy.sharing_description, for_=SHARING_FOR_ANY))
    if trusted is not None and trusted.sharing_description.strip():
        parts.append(_part("sharing", trusted.sharing_description, for_=counterparty_name))
    return parts


def sharing_text(parts: Sequence[str], *, counterparty_name: str) -> str:
    """`sharing_parts` as every prompt quotes them. With a trusted organization's
    description as well as the default, which applies where the two differ."""
    text = "\n\n".join(parts)
    if len(parts) < 2:
        return text
    return (
        f"{text}\n\nThe description for {counterparty_name} adds to the one for any "
        f"organization. Where the two differ, the one for {counterparty_name} applies."
    )


def also_passes(side: Side, counterparty_name: str, *, summary: bool = False) -> str:
    """What a representative may pass on besides the sharing descriptions: what answers
    from its side give it and, on the requester side, what the counterparty needs from
    the brief to answer it. With `summary` (no descriptions), the public summary too."""
    items = [
        *(["what its public summary says"] if summary else []),
        "what answers from your side give you",
        *(
            [f"what {counterparty_name} needs from your brief to answer it"]
            if side == "requester"
            else []
        ),
    ]
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _sharing_section(
    parts: Sequence[str], *, org_name: str, counterparty_name: str, side: Side
) -> str:
    if not parts:
        return (
            f"{org_name} hasn't described what you may pass on. Pass on only "
            f"{also_passes(side, counterparty_name, summary=True)}."
        )
    return (
        f"What {org_name} lets you pass on, in its own words. You may also pass on "
        f"{also_passes(side, counterparty_name)}. Anything else stays with your side.\n\n"
        + sharing_text(parts, counterparty_name=counterparty_name)
    )


def context_parts(system_text: str) -> list[ContextPart]:
    """The parts of a frozen system prompt (`rep_sessions.system_text`) that the
    context inspector lists, read back from the stored bytes in prompt order."""
    parts: list[ContextPart] = []
    for match in _PART.finditer(system_text):
        kind: ContextPartKind = _part_kind(match.group(1))
        attributes = dict(_ATTRIBUTE.findall(match.group(2)))
        parts.append(ContextPart(kind=kind, label=_label(kind, attributes), text=match.group(3)))
    return parts


async def load_rep_session(
    session: AsyncSession, *, world_id: UUID, thread_id: UUID, side: Side
) -> RepSession | None:
    """The frozen session for (thread, side), if its first turn has run."""
    return await session.get(RepSession, (world_id, thread_id, side))


async def freeze_rep_session(uow: UnitOfWork, context: RepContext) -> RepSession:
    """Store the side's system and tools bytes once and pin the thread side's settings
    version. If a session already exists (a retry, a race) the stored one wins and is
    returned unchanged."""
    if context.world_id != uow.world_id:
        raise ValueError(f"context of world {context.world_id} frozen in {uow.world_id}")
    session = uow.session
    inserted = await session.scalar(
        pg_insert(RepSession)
        .values(
            world_id=context.world_id,
            thread_id=context.thread_id,
            side=context.side,
            system_text=context.system,
            tools_text=canonical_json(list[JsonValue](context.tools)),
            model=context.model,
        )
        .on_conflict_do_nothing()
        .returning(RepSession.thread_id)
    )
    row = await session.get(
        RepSession, (context.world_id, context.thread_id, context.side), populate_existing=True
    )
    if row is None:
        raise LookupError(f"rep session ({context.thread_id}, {context.side}) vanished")
    if inserted is not None:
        await _pin_settings(uow, context)
    return row


async def queue_notice(
    uow: UnitOfWork,
    *,
    thread_id: UUID,
    side: Side,
    kind: NoticeKind,
    text: str,
    once: bool = False,
) -> RepNotice:
    """Queue a code-written notice for the side's next model call (§2.2). With `once`,
    a notice of the same kind and text the side already has, queued or sent, is
    returned instead (a retried tool call)."""
    body = text.strip()
    if not body:
        raise ValueError("a notice needs text")
    session = uow.session
    await _lock_notices(session, uow.world_id, thread_id, side)
    if once:
        queued = await session.scalar(
            select(RepNotice)
            .where(
                _notices_of(uow.world_id, thread_id, side),
                RepNotice.kind == kind,
                RepNotice.text == body,
            )
            .limit(1)
        )
        if queued is not None:
            return queued
    row = RepNotice(
        world_id=uow.world_id,
        thread_id=thread_id,
        side=side,
        seq=await _next_seq(session, uow.world_id, thread_id, side),
        kind=kind,
        text=body,
    )
    session.add(row)
    await session.flush()
    return row


async def flush_notices(
    uow: UnitOfWork, *, thread_id: UUID, side: Side, turn_id: UUID
) -> str | None:
    """The side's notices for `turn_id` as one system message text, None when there are
    none. The turn's first flush decides them and records that it ran; a repeat flush
    for the turn (a retried attempt) returns exactly the same.

    The first flush takes, in queue order, the notices flushed for an earlier turn that
    never stored its system message (its call was refused, incomplete or failed, or
    never made) and the notices queued since.
    """
    session = uow.session
    world_id = uow.world_id
    await _lock_notices(session, world_id, thread_id, side)
    stamped = await _notices(session, world_id, thread_id, side, turn_id)
    if stamped:
        return _merged(stamped)
    lost = await _undelivered(session, world_id, thread_id, side)
    pending = await _notices(session, world_id, thread_id, side, None)
    notices = sorted([*lost, *pending], key=lambda notice: notice.seq)
    for notice in notices:
        notice.flushed_turn_id = turn_id
    session.add(
        RepNotice(
            world_id=world_id,
            thread_id=thread_id,
            side=side,
            seq=await _next_seq(session, world_id, thread_id, side),
            kind=_FLUSHED,
            text="",
            flushed_turn_id=turn_id,
        )
    )
    await session.flush()
    return _merged(notices)


async def drop_undelivered(
    uow: UnitOfWork,
    *,
    thread_id: UUID,
    side: Side,
    kinds: Collection[NoticeKind] = ("closed", "reopened"),
) -> None:
    """Delete the side's notices of `kinds` that never reached its representative:
    unflushed, or flushed for a turn that stored no system message (REOPEN.md §5.3).
    A reopen drops them so the next run doesn't tell it the conversation closed, or
    reopened, again; other kinds stay true and stay queued."""
    session = uow.session
    world_id = uow.world_id
    await _lock_notices(session, world_id, thread_id, side)
    stale = [
        *await _notices(session, world_id, thread_id, side, None),
        *await _undelivered(session, world_id, thread_id, side),
    ]
    for notice in stale:
        if notice.kind in kinds:
            await session.delete(notice)
    await session.flush()


async def delivered_notices(
    session: AsyncSession, world_id: UUID, thread_id: UUID, side: Side
) -> list[DeliveredNotice]:
    """The side's notices that reached its representative, in queue order, each with
    when its call's system message was stored."""
    stored = (
        select(RepTranscript.turn_id, func.min(RepTranscript.created_at).label("at"))
        .where(
            RepTranscript.world_id == world_id,
            RepTranscript.thread_id == thread_id,
            RepTranscript.side == side,
            RepTranscript.role == "system",
        )
        .group_by(RepTranscript.turn_id)
        .subquery()
    )
    rows = await session.execute(
        select(RepNotice.kind, RepNotice.text, stored.c.at)
        .join(stored, stored.c.turn_id == RepNotice.flushed_turn_id)
        .where(_notices_of(world_id, thread_id, side), RepNotice.kind != _FLUSHED)
        .order_by(RepNotice.seq)
    )
    return [
        DeliveredNotice.model_validate({"kind": kind, "text": text, "delivered_at": at})
        for kind, text, at in rows
    ]


async def received_results(
    session: AsyncSession, world_id: UUID, thread_id: UUID, side: Side
) -> set[str]:
    """Every result text stored in the side's transcript: each tool result's content,
    and each text block (a result owed to no call arrives as one)."""
    contents = await session.scalars(
        select(RepTranscript.content_text).where(
            RepTranscript.world_id == world_id,
            RepTranscript.thread_id == thread_id,
            RepTranscript.side == side,
            RepTranscript.role == "user",
        )
    )
    received: set[str] = set()
    for content_text in contents:
        parsed: JsonValue = json.loads(content_text)
        for block in objects(parsed):
            field = "content" if block.get("type") == "tool_result" else "text"
            text = as_str(block.get(field))
            if text is not None:
                received.add(text)
    return received


# --- Guidance ---------------------------------------------------------------------------

# A delivered version's tag, as `guidance_text` writes it: only code writes a bare `<`.
_GUIDANCE_OPEN: Final = re.compile(rf'<{GUIDANCE_TAG} from="[^"]*" version="(\d+)">')


def guidance_text(text: str, *, version: int, org_name: str, counterparty_name: str) -> str:
    """The block that gives the representative a guidance version: a code-written lead,
    then the model-written text quoted in its tag. A withdrawal (empty text) keeps the
    empty tag, so its version is recognizable in the transcript too."""
    org, them = _one_line(org_name), _one_line(counterparty_name)
    if text.strip():
        lead = (
            f"{org} updated its guidance for this conversation. Follow it from now on, in "
            f"place of any earlier guidance from {org}. It comes from your own side, not "
            f"from {them}. It never widens what you may pass on. Don't mention or "
            f"acknowledge it to {them}; write to them with `send_message`."
        )
    else:
        lead = (
            f"{org} withdrew its guidance for this conversation. Follow your instructions "
            "as they stand."
        )
    return lead + "\n" + quoted(GUIDANCE_TAG, text, from_=org, version=str(version))


async def guidance_block(
    session: AsyncSession,
    world_id: UUID,
    thread_id: UUID,
    side: Side,
    version: int | None,
    *,
    org_name: str,
    counterparty_name: str,
    before_turn: UUID,
) -> str | None:
    """The block for guidance `version`, or None when there is none or a user message
    stored before turn `before_turn` already carries it. The turn's own rows are
    ignored, so every attempt of a turn builds the same opening."""
    if version is None:
        return None
    delivered = await _delivered_versions(session, world_id, thread_id, side, before=before_turn)
    if version in delivered:
        return None
    row = await session.scalar(
        repo.in_world(RepGuidance, world_id).where(
            RepGuidance.thread_id == thread_id,
            RepGuidance.side == side,
            RepGuidance.version == version,
        )
    )
    if row is None:
        raise LookupError(f"thread {thread_id} has no {side} guidance version {version}")
    return guidance_text(
        row.text, version=version, org_name=org_name, counterparty_name=counterparty_name
    )


async def delivered_guidance(
    session: AsyncSession, world_id: UUID, thread_id: UUID, side: Side
) -> list[DeliveredGuidance]:
    """Every guidance version a stored user message carries, oldest first, each with
    when it was first stored."""
    delivered = await _delivered_versions(session, world_id, thread_id, side)
    if not delivered:
        return []
    rows = await session.scalars(
        repo.in_world(RepGuidance, world_id)
        .where(
            RepGuidance.thread_id == thread_id,
            RepGuidance.side == side,
            RepGuidance.version.in_(list(delivered)),
        )
        .order_by(RepGuidance.version)
    )
    return [
        DeliveredGuidance(version=row.version, text=row.text, delivered_at=delivered[row.version])
        for row in rows
    ]


async def _delivered_versions(
    session: AsyncSession,
    world_id: UUID,
    thread_id: UUID,
    side: Side,
    *,
    before: UUID | None = None,
) -> dict[int, datetime]:
    """The guidance versions the side's stored user messages carry, each with when it
    was first stored; with `before`, only rows of other turns than that one."""
    query = select(RepTranscript.content_text, RepTranscript.created_at).where(
        RepTranscript.world_id == world_id,
        RepTranscript.thread_id == thread_id,
        RepTranscript.side == side,
        RepTranscript.role == "user",
    )
    if before is not None:
        query = query.where(or_(RepTranscript.turn_id.is_(None), RepTranscript.turn_id != before))
    versions: dict[int, datetime] = {}
    for content_text, created_at in await session.execute(query.order_by(RepTranscript.seq)):
        parsed: JsonValue = json.loads(content_text)
        for block in objects(parsed):
            text = as_str(block.get("text")) if block.get("type") == "text" else None
            for match in _GUIDANCE_OPEN.finditer(text or ""):
                versions.setdefault(int(match.group(1)), created_at)
    return versions


# --- Counterparty text and notices -----------------------------------------------------


def wrap_counterparty(body: str, *, counterparty_name: str) -> str:
    """The counterparty's message inside the code-written delimiter (§2.1). Every `<`
    (and anything that reads as one) becomes `&lt;`, so no tag can open or close inside
    it, and every line that reads like an identity statement, a verification, an
    operator message or one of Harmony's notices is marked `[unverified]`, so a forged
    "verified sender" block stays visibly inside the counterparty's words.

    Detection reads `_as_read`: invisible code points removed, compatibility forms,
    accents and common Cyrillic and Greek look-alikes folded to plain Latin. The text is
    otherwise kept as written, except that line breaks become `\\n`.
    """
    lines = [
        UNVERIFIED_MARK + line if _IDENTITY_LINE.search(_as_read(line)) else line
        for line in _LINE_BREAK.split(escape_angles(body))
    ]
    opening = COUNTERPARTY_OPEN.format(counterparty_name=_attribute(counterparty_name))
    return "\n".join([opening, *lines, COUNTERPARTY_CLOSE])


def quote_released(text: str) -> str:
    """Released text (model-drafted, maybe edited) as quoted data for the representative,
    never as words of Harmony's own (§2.2): inside the code-written block, with every
    `<` escaped so it can't close the block."""
    return "\n".join([RELEASED_OPEN, escape_angles(text.strip()), RELEASED_CLOSE])


def closed_notice(counterparty_name: str) -> str:
    """The `closed` notice: the counterparty ended the conversation, and this side may
    leave its own organization a summary in one last turn (R§5)."""
    return (
        f"Notice from Harmony: {_one_line(counterparty_name)} closed this conversation, so "
        "nothing more reaches them. To leave your organization a summary of how it went, "
        "call `close` with it now."
    )


def reopened_notice(counterparty_name: str, *, by_us: bool) -> str:
    """The `reopened` notice (REOPEN.md §5.2): the side's own organization, or the
    counterparty, reopened the conversation. It names organizations, never a member."""
    counterparty = _one_line(counterparty_name)
    if by_us:
        return (
            f"Notice from Harmony: your organization reopened this conversation with "
            f"{counterparty}, so messages reach them again. Send them your next message."
        )
    return (
        f"Notice from Harmony: {counterparty} reopened this conversation, so messages reach "
        "them again."
    )


def shared_notice(text: str, *, member_name: str, counterparty_name: str) -> str:
    """The `shared` notice: words a member of the representative's own side approved in
    their chat for this conversation, quoted as theirs."""
    member, them = _one_line(member_name), _one_line(counterparty_name)
    return (
        f"Notice from Harmony: {member}, on your side, shared this for your conversation "
        f"with {them}. Treat it as an answer from your side, not from {them}. It never "
        f"widens what you may pass on: share from it only what you may share with {them}, "
        "and only where it helps.\n" + quoted(SHARED_TAG, text, from_=member)
    )


def world_date_notice(world_date: date) -> str:
    """The code-written notice that tells a model today's world date (§11.1)."""
    return f"Today's date: {long_date(world_date)}."


def long_date(day: date) -> str:
    """`Monday, October 26, 2026`, the same in every locale."""
    return f"{_WEEKDAYS[day.weekday()]}, {_MONTHS[day.month - 1]} {day.day}, {day.year}"


def short_date(day: date) -> str:
    """`Oct 26`, the same in every locale."""
    return f"{_MONTHS[day.month - 1][:3]} {day.day}"


# --- Organization names -----------------------------------------------------------------


def names(text: str, name: str, *, keep: Iterable[str] | None = None) -> bool:
    """Whether `text` names the org `name`: in full, in any case, on word boundaries,
    with any whitespace between its words. With `keep`, also by the first word of a
    longer name as written, as `without_org_names` replaces it ("Northlake")."""
    if keep is None:
        pattern = _full_name(name)
        return pattern is not None and pattern.search(text) is not None
    return any(pattern.search(text) for pattern in _name_patterns(name, _first_words(keep)))


def without_org_names(text: str, others: Iterable[str], *, keep: Iterable[str] = ()) -> str:
    """`text` with every name in `others` replaced by `ANOTHER_ORG`: in full, and by the
    first word of a longer name as written ("Ridgeline" for Ridgeline Fabrication),
    unless a name in `keep` starts with the same word. What a member wrote for one
    conversation, before another sees it."""
    kept = _first_words(keep)
    for name in sorted(set(others), key=len, reverse=True):
        for pattern in _name_patterns(name, kept):
            text = pattern.sub(ANOTHER_ORG, text)
    return text


def _full_name(name: str) -> re.Pattern[str] | None:
    words = name.split()
    if not words:
        return None
    joined = r"\s+".join(re.escape(word) for word in words)
    return re.compile(rf"(?<!\w){joined}(?!\w)", re.IGNORECASE)


def _name_patterns(name: str, kept_first_words: Collection[str]) -> list[re.Pattern[str]]:
    full = _full_name(name)
    if full is None:
        return []
    patterns = [full]
    words = name.split()
    first = words[0]
    if (
        len(words) > 1
        and first[:1].isupper()
        and first not in _COMMON_FIRST_WORDS
        and first not in kept_first_words
    ):
        patterns.append(re.compile(rf"(?<!\w){re.escape(first)}(?!\w)"))
    return patterns


def _first_words(org_names: Iterable[str]) -> set[str]:
    return {name.split()[0] for name in org_names if name.split()}


def counterparty_descriptor(kind: str, communities: Sequence[str]) -> str:
    """The identity statement's parenthetical, from network-held facts only (the org's
    kind and its verified communities), never from the counterparty's own text."""
    words = " ".join(re.findall(r"[a-z0-9]+", kind.lower())) or "member"
    article = "an" if words[0] in "aeiou" else "a"
    text = f"{article} {words} organization on Harmony"
    if communities:
        text += ", a verified member of " + _joined([_one_line(c) for c in communities])
    return text


# --- Prompt parts ---------------------------------------------------------------------


def quoted(tag: str, text: str, **attributes: str) -> str:
    """Text a model or another party wrote, as data in a code-written tag: every `<`
    (and anything that reads as one) is escaped, so nothing inside can open or close a
    tag, and attribute values are sanitized like the delimiter's (`for_` renders
    `for`)."""
    attrs = "".join(
        f' {name.rstrip("_")}="{_attribute(value)}"' for name, value in attributes.items()
    )
    return f"<{tag}{attrs}>\n{escape_angles(text.strip())}\n</{tag}>"


def _part(kind: ContextPartKind, text: str, **attributes: str) -> str:
    """`text` in its part's tag (`quoted`), so `context_parts` finds the end."""
    return quoted(kind, text.strip() or EMPTY_PART, **attributes)


def _part_kind(tag: str) -> ContextPartKind:
    match tag:
        case "counterparty" | "public_summary" | "sharing" | "brief" | "ask" | "instructions":
            return tag
        case _:
            raise ValueError(f"unknown prompt part {tag!r}")


def _label(kind: ContextPartKind, attributes: dict[str, str]) -> str:
    match kind:
        case "counterparty":
            return "Who it's talking to"
        case "public_summary":
            return "Public summary"
        case "sharing":
            audience = attributes.get("for", SHARING_FOR_ANY)
            if audience == SHARING_FOR_ANY:
                return "What it may pass on"
            return f"Also for {audience}"
        case "brief":
            approved_by = attributes.get("approved_by")
            written_for = attributes.get("written_for")
            if approved_by:
                return f"Brief approved by {approved_by}"
            if written_for:
                return (
                    f"Brief written by Claude for {written_for}, sent without their review "
                    "(this chat's setting)"
                )
            return "Brief"
        case "ask":
            audience = attributes.get("for")
            return f"What it asks {audience}" if audience else "What it asks"
        case "instructions":
            return "Instructions"


def _joined(parts: Sequence[str]) -> str:
    if len(parts) <= 2:
        return " and ".join(parts)
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _visible(text: str) -> str:
    """`text` without the code points that render as nothing (`_IGNORABLE`, `_UNSEEN`)
    and without control characters other than whitespace."""
    return "".join(
        ch
        for ch in _IGNORABLE.sub("", text)
        if not (
            unicodedata.category(ch) in _UNSEEN
            or (unicodedata.category(ch) == "Cc" and not ch.isspace())
        )
    )


def _as_read(text: str) -> str:
    """What detection reads: the visible characters, decomposed (NFKD) without combining
    marks, with Cyrillic and Greek look-alikes read as Latin."""
    decomposed = unicodedata.normalize("NFKD", _visible(text))
    bare = "".join(ch for ch in decomposed if unicodedata.category(ch) not in _MARKS)
    return _visible(bare).translate(_LOOKALIKES)


@functools.lru_cache(maxsize=4096)
def _reads_as(ch: str) -> str:
    return _as_read(ch)


def escape_angles(text: str) -> str:
    """Every `<`, and anything that reads as one (a fullwidth or small form), as
    `&lt;`: quoted text can then neither open nor close a tag."""
    return "".join(ESCAPED_LESS_THAN if "<" in _reads_as(ch) else ch for ch in text)


def _attribute(name: str) -> str:
    """A name as a tag attribute: one line of visible characters, no quote or angle
    bracket."""
    text = _one_line(_visible(unicodedata.normalize("NFKC", _visible(name))))
    return "".join(
        "'" if '"' in _reads_as(ch) else ch
        for ch in text
        if "<" not in _reads_as(ch) and ">" not in _reads_as(ch)
    )


# --- Internal Claude -----------------------------------------------------------------


async def build_internal_context(
    session: AsyncSession, *, world_id: UUID, conversation_id: UUID
) -> InternalContext:
    """Render internal Claude's prompt (org, member name and title) and its tools."""
    conversation = await repo.get_required(session, Conversation, world_id, conversation_id)
    org = await repo.get_required(session, Org, world_id, conversation.org_id)
    member = await repo.get_required(session, Member, world_id, conversation.owner_member_id)
    org_name = _one_line(org.name)
    title = _one_line(member.title or "")
    system = render(
        "internal",
        {
            "org_name": org_name,
            "member_name": _one_line(member.name),
            "member_title": title or f"a member of {org_name}",
        },
    )
    return InternalContext(
        world_id=world_id,
        conversation_id=conversation_id,
        org_id=org.id,
        member_id=member.id,
        system=system,
        tools=list(_internal_tools()),
    )


# --- Loading -------------------------------------------------------------------------


# The tool modules import the domain layer and `agents.consult`, which imports this
# module (`quote_released`), so their definitions are read at call time.
def _rep_tools() -> tuple[ToolDefinition, ...]:
    from harmony.agents.tools.rep import REP_TOOLS

    return REP_TOOLS


def _internal_tools() -> tuple[ToolDefinition, ...]:
    from harmony.agents.tools.internal import INTERNAL_TOOLS

    return INTERNAL_TOOLS


def _parties(thread: Thread, side: Side) -> tuple[UUID, UUID]:
    """(this side's org, the counterparty)."""
    if side == "requester":
        return thread.requester_org_id, thread.responder_org_id
    return thread.responder_org_id, thread.requester_org_id


async def current_settings(session: AsyncSession, org: Org) -> RepSettings:
    """The org's current settings version (a new representative freezes it; the
    answerer reads it live)."""
    settings = await session.get(RepSettings, (org.world_id, org.id, org.current_settings_version))
    if settings is None:
        raise LookupError(f"org {org.id} has no settings version {org.current_settings_version}")
    return settings


async def _member_name(session: AsyncSession, world_id: UUID, member_id: UUID | None) -> str | None:
    if member_id is None:
        return None
    member = await repo.get(session, Member, world_id, member_id)
    return _one_line(member.name) if member is not None else None


async def _verified_communities(session: AsyncSession, world_id: UUID, org_id: UUID) -> list[str]:
    rows = await session.scalars(
        select(Community.name)
        .join(
            OrgCommunity,
            and_(
                OrgCommunity.world_id == Community.world_id,
                OrgCommunity.community_id == Community.id,
            ),
        )
        .where(
            Community.world_id == world_id,
            OrgCommunity.org_id == org_id,
            OrgCommunity.verified.is_(True),
        )
        .order_by(Community.name)
    )
    return list(rows)


async def _pin_settings(uow: UnitOfWork, context: RepContext) -> None:
    thread = await uow.session.get(
        Thread, (uow.world_id, context.thread_id), with_for_update=True, populate_existing=True
    )
    if thread is None:
        raise repo.NotFoundInWorld(Thread, uow.world_id, context.thread_id)
    version = context.settings_version
    if context.side == "requester":
        if thread.requester_settings_version == version:
            return
        thread.requester_settings_version = version
    else:
        if thread.responder_settings_version == version:
            return
        thread.responder_settings_version = version
    await uow.session.flush()
    for org_id in (thread.requester_org_id, thread.responder_org_id):
        uow.add_snapshot(thread, EventScope.thread(org_id, thread.id))


# --- Notices -------------------------------------------------------------------------


def _notices_of(world_id: UUID, thread_id: UUID, side: Side) -> ColumnElement[bool]:
    return and_(
        RepNotice.world_id == world_id, RepNotice.thread_id == thread_id, RepNotice.side == side
    )


async def _lock_notices(session: AsyncSession, world_id: UUID, thread_id: UUID, side: Side) -> None:
    """Serializes queueing and flushing for one side, so seqs and stamps can't race."""
    key = f"rep_notices:{world_id}:{thread_id}:{side}"
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


async def _notices(
    session: AsyncSession, world_id: UUID, thread_id: UUID, side: Side, turn_id: UUID | None
) -> list[RepNotice]:
    """The side's notices stamped with `turn_id`, or the unflushed ones (None)."""
    stamp = (
        RepNotice.flushed_turn_id.is_(None)
        if turn_id is None
        else RepNotice.flushed_turn_id == turn_id
    )
    query = (
        select(RepNotice)
        .where(_notices_of(world_id, thread_id, side), stamp)
        .order_by(RepNotice.seq)
        .execution_options(populate_existing=True)
    )
    return list(await session.scalars(query))


def _merged(notices: Sequence[RepNotice]) -> str | None:
    texts = [notice.text for notice in notices if notice.kind != _FLUSHED]
    return _NOTICE_SEPARATOR.join(texts) or None


async def _next_seq(session: AsyncSession, world_id: UUID, thread_id: UUID, side: Side) -> int:
    last = await session.scalar(
        select(func.max(RepNotice.seq)).where(_notices_of(world_id, thread_id, side))
    )
    return (last or 0) + 1


async def _undelivered(
    session: AsyncSession, world_id: UUID, thread_id: UUID, side: Side
) -> list[RepNotice]:
    """Notices flushed for a turn whose system message never reached the transcript
    (`agents.runtime` stores it with the turn's first response)."""
    delivered = (
        select(RepTranscript.seq)
        .where(
            RepTranscript.world_id == RepNotice.world_id,
            RepTranscript.thread_id == RepNotice.thread_id,
            RepTranscript.side == RepNotice.side,
            RepTranscript.turn_id == RepNotice.flushed_turn_id,
            RepTranscript.role == "system",
        )
        .exists()
    )
    query = (
        select(RepNotice)
        .where(
            _notices_of(world_id, thread_id, side),
            RepNotice.flushed_turn_id.is_not(None),
            RepNotice.kind != _FLUSHED,
            ~delivered,
        )
        .order_by(RepNotice.seq)
        .execution_options(populate_existing=True)
    )
    return list(await session.scalars(query))


__all__ = [
    "ANOTHER_ORG",
    "COUNTERPARTY_CLOSE",
    "COUNTERPARTY_OPEN",
    "EMPTY_PART",
    "ESCAPED_LESS_THAN",
    "GUIDANCE_TAG",
    "RELEASED_CLOSE",
    "RELEASED_OPEN",
    "SHARED_TAG",
    "SHARING_FOR_ANY",
    "UNVERIFIED_MARK",
    "DeliveredGuidance",
    "DeliveredNotice",
    "InternalContext",
    "NoticeKind",
    "RepContext",
    "also_passes",
    "build_internal_context",
    "build_rep_context",
    "closed_notice",
    "context_parts",
    "counterparty_descriptor",
    "current_settings",
    "delivered_guidance",
    "delivered_notices",
    "drop_undelivered",
    "escape_angles",
    "flush_notices",
    "freeze_rep_session",
    "guidance_block",
    "guidance_text",
    "load_rep_session",
    "long_date",
    "names",
    "queue_notice",
    "quote_released",
    "quoted",
    "received_results",
    "recipient_ask",
    "reopened_notice",
    "sent_without_review",
    "shared_notice",
    "sharing_parts",
    "sharing_text",
    "short_date",
    "without_org_names",
    "world_date_notice",
    "wrap_counterparty",
]
