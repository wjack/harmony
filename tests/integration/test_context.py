"""`agents.context` on a seeded harmony-demo world: what a representative's frozen prompt
holds, how notices reach it, internal Claude's prompt, and the firewall property over
the serialized bytes of every representative request (REVISION_UNSTRUCTURED.md §2, §11).
"""

from __future__ import annotations

import json
import re
import string
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Final, Literal, cast
from uuid import UUID, uuid4

import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st
from pydantic import JsonValue
from sqlalchemy import select
from temporalio.client import Client
from temporalio.testing import ActivityEnvironment

from harmony.agents import context, release
from harmony.agents.consult import (
    HELD_DECLINED,
    DraftReady,
    Escalated,
    answer,
    check_sharing,
    settle,
    settled_result,
)
from harmony.agents.context import RELEASED_CLOSE, RELEASED_OPEN
from harmony.agents.models import (
    CardResolution,
    ContextPart,
    EscalationResolution,
    OutreachCardPayload,
    OutreachEdits,
    OutreachRecipient,
)
from harmony.agents.runtime import (
    ConsultOpened,
    MessageHeld,
    MessageSent,
    OwedResult,
    text_block,
)
from harmony.agents.tools.internal import INTERNAL_TOOLS
from harmony.agents.tools.rep import REP_TOOLS
from harmony.agents.transcript import RepSideTranscript, SqlTranscriptStore, WireMessage
from harmony.db.models import (
    Conversation,
    Document,
    KnowledgeItem,
    NetworkRequest,
    Org,
    RepPolicy,
    RepSettings,
    Thread,
    ThreadMessage,
    TrustedOrganization,
)
from harmony.db.models.choices import Autonomy, Side
from harmony.db.uow import unit_of_work
from harmony.domain import cards, consults, conversations, requests, threads
from harmony.domain import settings as org_settings
from harmony.domain.settings import SETTINGS_NOTICE, SettingsEdit, TrustedEdit
from harmony.events.bus import StreamMessage
from harmony.llm.budget import FreeSpend
from harmony.llm.scripted import CassetteEntry, CassetteKey, ScriptedBlock, ScriptedModel
from harmony.llm.wire import JsonObject, canonical_json, objects
from harmony.temporal.activities.agents import AgentActivities, RepTurnInput, RepTurnResult
from harmony.temporal.activities.network import ApplyResolutionInput, NetworkActivities
from tests.integration._cassettes import entry
from tests.integration._e2_world import (
    ALEX,
    DANA,
    HELEN,
    HENDERSON,
    NORTHLAKE,
    RIDGELINE,
    Opened,
    Sessionmaker,
    open_thread,
    seeded_world,
    to_move,
)
from tests.unit._turns import Checkpointed, say, use

MODEL: Final = "claude-opus-5-5"
ORG_SLUG: Final = "northlake"
HENDERSON_NAME: Final = "Henderson Lab"
CASSETTE_THREAD: Final = "henderson.northlake"
# A record's body in the firewall property: unique to it, and unchanged by JSON.
MARKER: Final = re.compile(r"RECORD\[[0-9a-f]{32}\]")
# Quoted text a side released to its representative (`context.quote_released`).
RELEASED_SPAN: Final = re.compile(
    re.escape(RELEASED_OPEN) + ".*?" + re.escape(RELEASED_CLOSE), re.DOTALL
)
# The draft a sharing check judges: the one place its request may hold a record's body.
DRAFT_SPAN: Final = re.compile(r"<answer>.*?</answer>", re.DOTALL)


@pytest.fixture
async def world(db_sessionmaker: Sessionmaker) -> UUID:
    return await seeded_world(db_sessionmaker)


async def build(sm: Sessionmaker, opened: Opened, side: Side = "responder") -> context.RepContext:
    async with sm() as session:
        return await context.build_rep_context(
            session, world_id=opened.world_id, thread_id=opened.thread_id, side=side, model=MODEL
        )


async def freeze(sm: Sessionmaker, opened: Opened) -> context.RepContext:
    """Northlake's side frozen, as its first turn freezes it."""
    built = await build(sm, opened)
    async with unit_of_work(sm, opened.world_id) as uow:
        await context.freeze_rep_session(uow, built)
        await uow.commit()
    return built


async def flush(sm: Sessionmaker, opened: Opened, turn_id: UUID) -> str | None:
    async with unit_of_work(sm, opened.world_id) as uow:
        text = await context.flush_notices(
            uow, thread_id=opened.thread_id, side="responder", turn_id=turn_id
        )
        await uow.commit()
    return text


async def queue(sm: Sessionmaker, opened: Opened, text: str) -> None:
    async with unit_of_work(sm, opened.world_id) as uow:
        await context.queue_notice(
            uow, thread_id=opened.thread_id, side="responder", kind="settings", text=text
        )
        await uow.commit()


async def terms(sm: Sessionmaker, world_id: UUID, org_id: UUID) -> tuple[RepSettings, RepPolicy]:
    """The org's current settings and policy."""
    async with sm() as session:
        org = await session.get(Org, (world_id, org_id))
        assert org is not None
        current = await context.current_settings(session, org)
        policy = await session.get(RepPolicy, (world_id, org_id))
        assert policy is not None
        return current, policy


async def mode_toward_henderson(sm: Sessionmaker, world_id: UUID) -> Autonomy:
    async with sm() as session:
        policy = await session.get(RepPolicy, (world_id, NORTHLAKE))
        assert policy is not None
        trusted = await session.get(TrustedOrganization, (world_id, NORTHLAKE, HENDERSON))
        return release.mode_for(policy, trusted)


async def record_bodies(sm: Sessionmaker, world_id: UUID) -> list[str]:
    async with sm() as session:
        knowledge = await session.scalars(
            select(KnowledgeItem.body).where(KnowledgeItem.world_id == world_id)
        )
        documents = await session.scalars(
            select(Document.text).where(Document.world_id == world_id, Document.text != "")
        )
        return [*knowledge, *documents]


def side_of(opened: Opened) -> RepSideTranscript:
    return RepSideTranscript(world_id=opened.world_id, thread_id=opened.thread_id, side="responder")


async def store_turn(sm: Sessionmaker, opened: Opened, turn_id: UUID) -> None:
    """What `run_turn` stores for a turn whose first call went out with its notices."""
    await SqlTranscriptStore(sm).append(
        side_of(opened),
        [
            WireMessage.of("user", [text_block("Hello")]),
            WireMessage.of("system", [text_block("Notices")]),
            WireMessage.of("assistant", [text_block("Hi")]),
        ],
        turn_id=turn_id,
    )


async def counterparty_says(sm: Sessionmaker, opened: Opened, body: str) -> str:
    """Henderson's representative sends `body`: Northlake's side is to move."""
    async with unit_of_work(sm, opened.world_id) as uow:
        await threads.append_message(
            uow,
            opened.thread_id,
            message_id=uuid4(),
            from_org_id=HENDERSON,
            kind="message",
            body=body,
            provenance="agent",
            turn_id=uuid4(),
        )
        await uow.commit()
    return body


# --- The frozen prompt -----------------------------------------------------------------


async def test_the_responder_prompt_holds_its_parts_and_no_record(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    opened = await open_thread(db_sessionmaker, world)
    built = await build(db_sessionmaker, opened)
    current, policy = await terms(db_sessionmaker, world, NORTHLAKE)

    parts = context.context_parts(built.system)
    assert [(part.kind, part.label) for part in parts] == [
        ("counterparty", "Who it's talking to"),
        ("public_summary", "Public summary"),
        ("sharing", "What it may pass on"),
        ("instructions", "Instructions"),
    ]
    assert parts[0].text.startswith(
        "Henderson Lab (a research organization on Harmony, a verified member of Northlake "
        'University), represented by its own agent, "Claude for Henderson Lab".'
    )
    assert [part.text for part in parts[1:]] == [
        current.public_summary.strip(),
        policy.sharing_description.strip(),
        current.instructions.strip(),
    ]
    assert built.system.startswith("You are Claude for Northlake Characterization Core")
    # An answer a teammate released may be passed on, whatever the description says.
    assert (
        "What Northlake Characterization Core lets you pass on, in its own words. You may "
        "also pass on what answers from your side give you. Anything else stays with your "
        "side." in built.system
    )
    async with db_sessionmaker() as session:
        request = await session.get(NetworkRequest, (world, opened.request_id))
        assert request is not None
    assert request.brief not in built.system
    bodies = await record_bodies(db_sessionmaker, world)
    assert bodies
    assert not [body for body in bodies if body in built.system]
    assert built.tools == [dict(tool) for tool in REP_TOOLS]


async def test_the_requester_prompt_holds_the_brief_its_member_approved(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    opened = await open_thread(db_sessionmaker, world)
    built = await build(db_sessionmaker, opened, "requester")
    async with db_sessionmaker() as session:
        request = await session.get(NetworkRequest, (world, opened.request_id))
        assert request is not None

    parts = context.context_parts(built.system)
    assert [part.kind for part in parts] == [
        "counterparty",
        "public_summary",
        "sharing",
        "brief",
        "ask",
        "instructions",
    ]
    assert parts[0].text.startswith(
        "Northlake Characterization Core (a characterization organization on Harmony, a "
        "verified member of Northlake University)"
    )
    assert parts[3] == ContextPart(
        kind="brief", label="Brief approved by Alex Chen", text=request.brief
    )
    # A request from before per-recipient asks: the ask is the brief's own request.
    assert parts[4] == ContextPart(
        kind="ask",
        label="What it asks Northlake Characterization Core",
        text="What the brief needs from Northlake Characterization Core.",
    )
    assert (
        "Alex Chen approved this brief and this ask for Northlake Characterization Core, and "
        "they are your mandate:" in built.system
    )
    # What Northlake needs from the brief is Henderson's to pass on, so the sharing
    # section mustn't keep it back.
    assert (
        "You may also pass on what answers from your side give you and what Northlake "
        "Characterization Core needs from your brief to answer it." in built.system
    )
    assert not [b for b in await record_bodies(db_sessionmaker, world) if b in built.system]


NORTHLAKE_ASK: Final = "Quote the XRD on 12 powders by Nov 6. Up to $1,600; keep it to yourself."
RIDGELINE_ASK: Final = "Quote an airtight holder by Nov 3. Up to $400; keep it to yourself."


async def sent_request(
    sm: Sessionmaker, world_id: UUID, *, auto: bool, edited_ask: str | None = None
) -> Opened:
    """Alex's chat proposes a request to Northlake and Ridgeline, each with its own ask;
    it goes out (Alex approving, maybe editing Northlake's ask, or without review), and
    Northlake's thread opens."""
    opened = await open_thread(sm, world_id)
    payload = OutreachCardPayload(
        title="Cathode characterization and holder",
        brief="Powder XRD on 12 air-sensitive powders by Nov 6, and a holder by Nov 3.",
        recipients=[
            OutreachRecipient(
                org_id=org_id, fit="strong", reason="Fits.", ask=ask, title=title, suggested=True
            )
            for org_id, ask, title in (
                (NORTHLAKE, NORTHLAKE_ASK, "XRD quote"),
                (RIDGELINE, RIDGELINE_ASK, "Holder quote"),
            )
        ],
        auto_send_recipients=[NORTHLAKE, RIDGELINE] if auto else None,
    )
    card_id = uuid4()
    edits = OutreachEdits(asks={NORTHLAKE: edited_ask}) if edited_ask else None
    async with unit_of_work(sm, world_id) as uow:
        card = await cards.create(
            uow,
            card_id=card_id,
            conversation_id=opened.conversation_id,
            turn_id=uuid4(),
            tool_use_id="toolu_request",
            payload=payload,
            auto=auto,
        )
        resolution = CardResolution(
            card_id=card.id, decision="approve", member_id=ALEX, edits=edits, auto=auto
        )
        card = await cards.resolve(uow, resolution)
        request = await requests.create_from_card(uow, card, resolution, request_id=uuid4())
        thread = await threads.open(
            uow, thread_id=uuid4(), request=request, responder_org_id=NORTHLAKE, deadline_at=None
        )
        await uow.commit()
    return Opened(world_id, thread.id, request.id, opened.conversation_id)


async def test_the_requester_prompt_asks_this_recipient_only_for_its_own_part(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    """3.13 E2: the ask as Alex edited it, and nothing of Ridgeline's."""
    edited = "Quote the XRD on 12 powders by Nov 6. Up to $1,500; keep it to yourself."
    opened = await sent_request(db_sessionmaker, world, auto=False, edited_ask=edited)

    built = await build(db_sessionmaker, opened, "requester")

    asks = [part for part in context.context_parts(built.system) if part.kind == "ask"]
    assert asks == [
        ContextPart(kind="ask", label="What it asks Northlake Characterization Core", text=edited)
    ]
    assert NORTHLAKE_ASK not in built.system
    assert RIDGELINE_ASK not in built.system
    assert "$400" not in built.system
    assert "Ask Northlake Characterization Core only for what the ask covers." in built.system
    assert "Brief approved by Alex Chen" in [p.label for p in context.context_parts(built.system)]


async def test_a_request_sent_without_review_says_so_in_the_requester_prompt(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    """3.10: the brief is still the mandate, but nobody reviewed it."""
    opened = await sent_request(db_sessionmaker, world, auto=True)

    built = await build(db_sessionmaker, opened, "requester")

    (brief,) = [part for part in context.context_parts(built.system) if part.kind == "brief"]
    assert brief.label == (
        "Brief written by Claude for Alex Chen, sent without their review (this chat's setting)"
    )
    assert "approved" not in brief.label
    assert "consult before committing to a price, a date or other terms" in built.system
    assert "Alex Chen approved" not in built.system


async def test_a_trusted_organization_also_gets_its_own_sharing_description(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    henderson = await open_thread(db_sessionmaker, world)
    ridgeline = await open_thread(db_sessionmaker, world, requester=RIDGELINE, sender=DANA)
    _, policy = await terms(db_sessionmaker, world, NORTHLAKE)
    async with db_sessionmaker() as session:
        trusted = await session.get(TrustedOrganization, (world, NORTHLAKE, RIDGELINE))
        assert trusted is not None

    def sharing(system: str) -> list[tuple[str, str]]:
        return [(p.label, p.text) for p in context.context_parts(system) if p.kind == "sharing"]

    default = ("What it may pass on", policy.sharing_description.strip())
    ridgeline_system = (await build(db_sessionmaker, ridgeline)).system
    henderson_system = (await build(db_sessionmaker, henderson)).system
    assert sharing(ridgeline_system) == [
        default,
        ("Also for Ridgeline Fabrication", trusted.sharing_description.strip()),
    ]
    assert sharing(henderson_system) == [default]
    # Where the two differ, the one for that organization applies.
    applies = "Where the two differ, the one for Ridgeline Fabrication applies."
    assert applies in ridgeline_system
    assert "Where the two differ" not in henderson_system


async def test_the_first_freeze_wins_and_pins_the_settings_version(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    opened = await open_thread(db_sessionmaker, world)
    built = await freeze(db_sessionmaker, opened)
    changed = built.model_copy(update={"system": "something else", "tools": []})
    async with unit_of_work(db_sessionmaker, world) as uow:
        again = await context.freeze_rep_session(uow, changed)
        stored = (again.system_text, again.tools_text)
        await uow.commit()

    assert stored == (built.system, canonical_json(list[JsonValue](built.tools)))
    current, _ = await terms(db_sessionmaker, world, NORTHLAKE)
    async with db_sessionmaker() as session:
        thread = await session.get(Thread, (world, opened.thread_id))
        assert thread is not None
        pinned = (thread.responder_settings_version, thread.requester_settings_version)
    assert pinned == (current.version, None)


async def test_a_settings_change_keeps_the_frozen_prompt_and_arrives_as_notices(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    opened = await open_thread(db_sessionmaker, world)
    built = await freeze(db_sessionmaker, opened)
    _, policy = await terms(db_sessionmaker, world, NORTHLAKE)
    edit = SettingsEdit(
        instructions="Quote every price in euros.",
        sharing_description="Only our <external> price list.",
        autonomy="skip",
    )
    async with unit_of_work(db_sessionmaker, world) as uow:
        await org_settings.update(
            uow, org_id=NORTHLAKE, member_id=HELEN, policy_version=policy.version, edit=edit
        )
        await uow.commit()

    text = await flush(db_sessionmaker, opened, uuid4())

    assert text is not None
    assert text.startswith(SETTINGS_NOTICE)
    assert org_settings.mode_notice("skip") in text
    # The new description is quoted as a part, read back exactly.
    assert context.context_parts(text) == [
        ContextPart(
            kind="sharing", label="What it may pass on", text="Only our &lt;external> price list."
        )
    ]
    assert "euros" not in text
    async with db_sessionmaker() as session:
        frozen = await context.load_rep_session(
            session, world_id=world, thread_id=opened.thread_id, side="responder"
        )
        assert frozen is not None
        assert frozen.system_text == built.system


# --- Notices ---------------------------------------------------------------------------


async def test_notices_flush_once_in_order_and_idempotently(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    opened = await open_thread(db_sessionmaker, world)
    await freeze(db_sessionmaker, opened)
    for text in ("First notice.", "Second notice."):
        await queue(db_sessionmaker, opened, text)
    first, second = uuid4(), uuid4()

    assert await flush(db_sessionmaker, opened, first) == "First notice.\n\nSecond notice."
    assert await flush(db_sessionmaker, opened, first) == "First notice.\n\nSecond notice."
    await store_turn(db_sessionmaker, opened, first)
    assert await flush(db_sessionmaker, opened, second) is None


async def test_a_turns_notices_are_decided_by_its_first_flush(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    """A repeat flush for a turn (a retried attempt) returns what the first returned,
    even when that was nothing; a notice queued in between waits for the next turn."""
    opened = await open_thread(db_sessionmaker, world)
    await freeze(db_sessionmaker, opened)
    turn = uuid4()
    assert await flush(db_sessionmaker, opened, turn) is None
    await queue(db_sessionmaker, opened, "Settings changed.")

    assert await flush(db_sessionmaker, opened, turn) is None
    assert await flush(db_sessionmaker, opened, uuid4()) == "Settings changed."


async def test_notices_a_refused_call_never_stored_go_out_again(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    opened = await open_thread(db_sessionmaker, world)
    await counterparty_says(db_sessionmaker, opened, "Can you run XRD by Nov 6?")
    await freeze(db_sessionmaker, opened)
    await queue(db_sessionmaker, opened, "Settings changed.")
    model = ScriptedModel(entry(ORG_SLUG, "representative", 1, 0, [], refused=True))
    turn = RepTurnInput(
        world_id=world,
        thread_id=opened.thread_id,
        side="responder",
        org_id=NORTHLAKE,
        cassette_thread=CASSETTE_THREAD,
        turn_id=uuid4(),
        turn=1,
    )
    refused = await run_rep_turn(db_sessionmaker, model, turn)
    assert refused.stop == "refusal"
    # The notice went out with the refused call, after the date a first turn opens with.
    [*_, sent] = objects(model.requests[-1].body.get("messages"))
    assert sent["role"] == "system"
    assert canonical_json(sent["content"]).endswith('Settings changed."}]')
    await queue(db_sessionmaker, opened, "Mode changed.")

    second = uuid4()
    assert await flush(db_sessionmaker, opened, second) == "Settings changed.\n\nMode changed."
    # Once a turn has stored them, nothing goes out a third time.
    await store_turn(db_sessionmaker, opened, second)
    assert await flush(db_sessionmaker, opened, uuid4()) is None


# --- Internal Claude ---------------------------------------------------------------------


async def test_internal_claude_knows_the_org_and_the_member(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    conversation_id = uuid4()
    async with unit_of_work(db_sessionmaker, world) as uow:
        uow.session.add(
            Conversation(world_id=world, id=conversation_id, org_id=HENDERSON, owner_member_id=ALEX)
        )
        await uow.commit()
    async with db_sessionmaker() as session:
        built = await context.build_internal_context(
            session, world_id=world, conversation_id=conversation_id
        )
    assert built.system.startswith("You are Claude, the AI assistant at Henderson Lab.")
    assert "You're talking with Alex Chen, PhD student." in built.system
    assert built.tools == [dict(tool) for tool in INTERNAL_TOOLS]
    assert (built.org_id, built.member_id) == (HENDERSON, ALEX)


async def test_internal_claudes_prompt_and_tools_are_the_same_in_either_send_mode(
    db_sessionmaker: Sessionmaker, world: UUID
) -> None:
    """3.10: the bytes stay the same across a chat's calls, whatever its setting."""
    sm = db_sessionmaker
    opened = await open_thread(sm, world)

    async def built() -> context.InternalContext:
        async with sm() as session:
            return await context.build_internal_context(
                session, world_id=world, conversation_id=opened.conversation_id
            )

    reviewed = await built()
    async with unit_of_work(sm, world) as uow:
        await conversations.set_send_mode(uow, opened.conversation_id, True)
        await uow.commit()
    unreviewed = await built()

    assert (unreviewed.system, unreviewed.tools) == (reviewed.system, reviewed.tools)


# --- §11 firewall: no record reaches a representative but in a released answer ------------

# The property's two threads, both to Northlake: (requester, the member who sent it).
REQUESTERS: Final = ((HENDERSON, ALEX), (RIDGELINE, DANA))
# The org slugs cassettes are keyed by, and who decides each org's items.
SLUGS: Final = {NORTHLAKE: "northlake", HENDERSON: "henderson", RIDGELINE: "ridgeline"}
DECIDER: Final = {NORTHLAKE: HELEN, HENDERSON: ALEX, RIDGELINE: DANA}
SIDES: Final[tuple[Side, ...]] = ("requester", "responder")
# Counterparty text as the representative receives it (`context.wrap_counterparty`).
COUNTERPARTY_SPAN: Final = re.compile(
    re.escape(context.COUNTERPARTY_OPEN.split("{")[0])
    + r'[^"]*">.*?'
    + re.escape(context.COUNTERPARTY_CLOSE),
    re.DOTALL,
)


@dataclass(frozen=True)
class Records:
    world_id: UUID
    # Each org's records as its answerer reads them (`kn:` and `doc:` IDs), with markers.
    by_org: dict[UUID, tuple[tuple[str, str], ...]]

    def owner(self, body: str) -> UUID:
        return next(org for org, rows in self.by_org.items() if body in (m for _, m in rows))


def marker(record_id: UUID) -> str:
    return f"RECORD[{record_id.hex}]"


@pytest.fixture
async def records(db_sessionmaker: Sessionmaker, world: UUID) -> Records:
    """Every knowledge item's body and document's text in the world replaced by a marker
    unique to it, with a document added for each org in the property."""
    async with unit_of_work(db_sessionmaker, world) as uow:
        session = uow.session
        for org_id, title in (
            (NORTHLAKE, "XRD sample-prep SOP"),
            (HENDERSON, "Synthesis log"),
            (RIDGELINE, "Wafer run sheet"),
        ):
            session.add(
                Document(
                    world_id=world,
                    id=uuid4(),
                    org_id=org_id,
                    title=title,
                    object_name=f"{title}.pdf",
                    mime="application/pdf",
                    size_bytes=1,
                )
            )
        await session.flush()
        items = (
            await session.scalars(select(KnowledgeItem).where(KnowledgeItem.world_id == world))
        ).all()
        documents = (
            await session.scalars(select(Document).where(Document.world_id == world))
        ).all()
        for item in items:
            item.body = marker(item.id)
        for document in documents:
            document.text = marker(document.id)
        by_org = {
            org_id: (
                *((f"kn:{i.id}", marker(i.id)) for i in items if i.org_id == org_id),
                *((f"doc:{d.id}", marker(d.id)) for d in documents if d.org_id == org_id),
            )
            for org_id in SLUGS
        }
        await uow.commit()
    return Records(world_id=world, by_org=by_org)


@dataclass(frozen=True)
class Seat:
    """One side of one of the property's threads."""

    thread: int
    side: Side

    @property
    def org_id(self) -> UUID:
        return REQUESTERS[self.thread][0] if self.side == "requester" else NORTHLAKE

    @property
    def counterparty_id(self) -> UUID:
        return Seat(self.thread, threads.other(self.side)).org_id

    @property
    def cassette_thread(self) -> str:
        return f"thread-{self.thread}"


type Action = Literal["send", "text", "consult", "close"]
type PersonAction = Literal["approve", "decline", "take_over"]
# Consults most often: they are what can carry a record. A close ends its thread.
ACTIONS: Final[tuple[Action, ...]] = (
    "send",
    "text",
    "consult",
    "consult",
    "send",
    "consult",
    "close",
)
PERSON_ACTIONS: Final[tuple[PersonAction, ...]] = ("approve", "decline", "take_over")
MODES: Final[tuple[Autonomy, ...]] = ("auto", "skip", "ask")


@dataclass(frozen=True)
class Answer:
    """How the answerer drafts one consult's answer, and what the person does when the
    consult (or, in *Ask*, the held message) reaches one."""

    # Indexes (modulo) into the seat's org's records: what the answerer reads and copies.
    reads: tuple[int, ...]
    invalid: bool
    needs_human: bool
    within_sharing: bool
    person: PersonAction
    # A record the person's text quotes, if any.
    quotes: int | None


@dataclass(frozen=True)
class Turn:
    seat: Seat
    action: Action
    refused: bool
    answer: Answer


@dataclass(frozen=True)
class PolicyChange:
    edit: SettingsEdit


@dataclass(frozen=True)
class TrustChange:
    autonomy: Autonomy | None
    sharing: str


type Step = Turn | PolicyChange | TrustChange

WORDS = st.text(alphabet=string.ascii_letters + " <>&\"'.", max_size=40)
ANSWERS = st.builds(
    Answer,
    reads=st.lists(st.integers(min_value=0, max_value=20), max_size=2).map(tuple),
    invalid=st.sampled_from([False, False, False, True]),
    needs_human=st.booleans(),
    within_sharing=st.booleans(),
    person=st.sampled_from(PERSON_ACTIONS),
    quotes=st.none() | st.integers(min_value=0, max_value=20),
)
TURNS = st.builds(
    Turn,
    seat=st.builds(Seat, st.integers(min_value=0, max_value=1), st.sampled_from(SIDES)),
    action=st.sampled_from(ACTIONS),
    refused=st.sampled_from([False, False, False, True]),
    answer=ANSWERS,
)
CHANGES: st.SearchStrategy[Step] = st.one_of(
    WORDS.map(lambda text: PolicyChange(SettingsEdit(sharing_description=text))),
    st.sampled_from(MODES).map(lambda mode: PolicyChange(SettingsEdit(autonomy=mode))),
    WORDS.map(lambda text: PolicyChange(SettingsEdit(instructions=text))),
    WORDS.map(lambda text: PolicyChange(SettingsEdit(public_summary=text))),
    st.builds(TrustChange, st.none() | st.sampled_from(MODES), WORDS),
)
# Two turns for every settings change (`one_of` would flatten CHANGES' branches).
STEPS: st.SearchStrategy[Step] = st.integers(min_value=0, max_value=2).flatmap(
    lambda kind: CHANGES if kind == 0 else TURNS
)
# What a person writes when their text quotes no record.
PERSON_TEXT: Final[dict[PersonAction, str]] = {
    "approve": "Nov 4 works.",
    "decline": "",
    "take_over": "Here: Nov 4 works.",
}
PLAIN: Final = Answer(
    reads=(), invalid=False, needs_human=False, within_sharing=True, person="approve", quotes=None
)


def opening_turns() -> list[Step]:
    """Each requester opens its thread, as the thread workflow starts it."""
    return [Turn(Seat(t, "requester"), "send", refused=False, answer=PLAIN) for t in (0, 1)]


def last_turns() -> list[Step]:
    """A message from every seat, so the last results owed reach a request."""
    return [
        Turn(Seat(t, side), "send", refused=False, answer=PLAIN) for t in (0, 1) for side in SIDES
    ]


def expected_effects(
    turn: Turn, mode: Autonomy, *, answers: bool
) -> list[type[ConsultOpened | MessageSent | MessageHeld]]:
    """What the turn's one call does; `answers` is whether the turn brought something
    to answer."""
    if turn.action == "consult":
        return [ConsultOpened]
    if turn.action == "text" and not answers:
        # Bare text after nothing new answers nobody: it stays private (3.13 E3).
        return []
    # In *Ask* a teammate approves every message first; a close sends no words.
    return [MessageHeld if mode == "ask" and turn.action != "close" else MessageSent]


def expected_stop(turn: Turn, mode: Autonomy, *, answers: bool) -> str:
    if turn.refused:
        return "refusal"
    held = MessageHeld in expected_effects(turn, mode, answers=answers)
    if turn.action in ("consult", "close") or held:
        return "performative"
    return "end_turn"


def pick(records: Records, org_id: UUID, index: int) -> tuple[str, str]:
    rows = records.by_org[org_id]
    return rows[index % len(rows)]


def person_quotes(records: Records, seat: Seat, plan: Answer) -> set[str]:
    """The record the person's own text quotes, if the plan has it quote one."""
    return set() if plan.quotes is None else {pick(records, seat.org_id, plan.quotes)[1]}


def consult_releases(records: Records, seat: Seat, plan: Answer, mode: Autonomy) -> set[str]:
    """The record bodies a consult may hand the representative, from the plan and §4.4
    alone, never from what the code under test returned: the draft (the records the
    answerer read) when it goes straight back, else what the person's text quotes."""
    straight_back = (
        not plan.invalid
        and not plan.needs_human
        and (mode == "skip" or (mode == "auto" and plan.within_sharing))
    )
    if straight_back:
        return {pick(records, seat.org_id, index)[1] for index in plan.reads}
    return person_quotes(records, seat, plan)


def rep_script(seat: Seat, n: int, turn: Turn) -> dict[CassetteKey, CassetteEntry]:
    """The seat's turn `n`: refused, or one call, then text once the model stops calling
    tools."""
    slug = SLUGS[seat.org_id]
    if turn.refused:
        return entry(slug, "representative", n, 0, [], refused=True)
    match turn.action:
        case "send":
            first = [use("send_message", f"toolu_{n}", text=f"Reply {n}.")]
        case "text":
            first = [say(f"Plain reply {n}.")]
        case "consult":
            first = [use("consult", f"toolu_{n}", question=f"Question {n}?")]
        case "close":
            first = [use("close", f"toolu_{n}", summary=f"Summary {n}.")]
    return entry(slug, "representative", n, 0, first) | entry(
        slug, "representative", n, 1, [say("Done.")]
    )


def last_word(seat: Seat, n: int) -> dict[CassetteKey, CassetteEntry]:
    """The seat's one turn after the counterparty closed: its summary."""
    return entry(
        SLUGS[seat.org_id],
        "representative",
        n,
        0,
        [use("close", f"toolu_{n}", summary="Their side closed it.")],
    )


def answerer_script(
    seat: Seat, n: int, plan: Answer, records: Records
) -> dict[CassetteKey, CassetteEntry]:
    """The consult the seat's turn `n` opens (keyed by the turn): the answerer reads its
    org's records, then drafts an answer that copies their bodies; and the sharing
    check's verdict, should the decision need one."""
    slug = SLUGS[seat.org_id]
    reads = [pick(records, seat.org_id, index) for index in plan.reads]
    steps: list[list[ScriptedBlock]] = []
    if reads:
        steps.append(
            [
                use("read_document", f"toolu_read_{n}_{i}", id=ref)
                for i, (ref, _) in enumerate(reads)
            ]
        )
    draft = {
        "answer": " ".join(["From our records:", *(body for _, body in reads)]),
        "needs_human": plan.needs_human,
        "needs_human_reason": "A teammate decides this." if plan.needs_human else None,
        "route": None,
        "rationale": "Read the records.",
    }
    steps.append([say("not a draft" if plan.invalid else json.dumps(draft))])
    script: dict[CassetteKey, CassetteEntry] = {}
    for step, content in enumerate(steps):
        script |= entry(slug, "answerer", n, step, content)
    verdict = {"within_sharing": plan.within_sharing, "reason": "Checked."}
    return script | entry(slug, "sharing_check", n, 0, [say(json.dumps(verdict))])


class Unfolding(ScriptedModel):
    """A `ScriptedModel` scripted turn by turn as the property's plan unfolds: which
    seat speaks after a close isn't known until the close is decided."""

    def add(self, script: dict[CassetteKey, CassetteEntry]) -> None:
        self._entries.update(script)


class NoStream:
    """No UI watches the property's turns."""

    async def publish(self, message: StreamMessage) -> None:
        return None

    async def aclose(self) -> None:
        return None


async def has_written(
    sm: Sessionmaker, opened: Opened, org_id: UUID, *, after_seq: int = 0
) -> bool:
    """Whether `org_id` has sent a message on the thread after `after_seq`."""
    async with sm() as session:
        found = await session.scalar(
            select(ThreadMessage.id)
            .where(
                ThreadMessage.world_id == opened.world_id,
                ThreadMessage.thread_id == opened.thread_id,
                ThreadMessage.from_org_id == org_id,
                ThreadMessage.seq > after_seq,
                ThreadMessage.kind == "message",
            )
            .limit(1)
        )
    return found is not None


def only_held_declines(owed: Sequence[OwedResult]) -> bool:
    """Whether every result owed is a held message declined without a reason: that
    leaves nothing to answer (3.13 E3)."""
    return bool(owed) and all(result.content == HELD_DECLINED for result in owed)


async def run_rep_turn(sm: Sessionmaker, model: ScriptedModel, turn: RepTurnInput) -> RepTurnResult:
    """One turn through the `rep_turn` activity itself: its prompt freeze, notices,
    opening and executor."""
    agents = AgentActivities(sm, model, FreeSpend(), NoStream(), model_override=MODEL)
    return await ActivityEnvironment().run(agents.rep_turn, turn)


async def mode_of(sm: Sessionmaker, world_id: UUID, seat: Seat) -> Autonomy:
    async with sm() as session:
        return await org_settings.mode_toward(
            session, world_id=world_id, org_id=seat.org_id, counterparty_org_id=seat.counterparty_id
        )


async def change(sm: Sessionmaker, world_id: UUID, step: PolicyChange | TrustChange) -> None:
    """Helen edits Northlake's settings (the domain queues the notices)."""
    async with unit_of_work(sm, world_id) as uow:
        session = uow.session
        match step:
            case PolicyChange(edit=edit):
                policy = await session.get(RepPolicy, (world_id, NORTHLAKE))
                assert policy is not None
                await org_settings.update(
                    uow, org_id=NORTHLAKE, member_id=HELEN, policy_version=policy.version, edit=edit
                )
            case TrustChange(autonomy=autonomy, sharing=sharing):
                row = await session.get(TrustedOrganization, (world_id, NORTHLAKE, HENDERSON))
                if row is None:
                    await org_settings.add_trusted(
                        uow,
                        org_id=NORTHLAKE,
                        member_id=HELEN,
                        counterparty_org_id=HENDERSON,
                        autonomy=autonomy,
                        sharing_description=sharing,
                    )
                else:
                    await org_settings.update_trusted(
                        uow,
                        org_id=NORTHLAKE,
                        member_id=HELEN,
                        counterparty_org_id=HENDERSON,
                        version=row.version,
                        edit=TrustedEdit(autonomy=autonomy, sharing_description=sharing),
                    )
        await uow.commit()


@pytest.fixture
async def seeded(db_sessionmaker: Sessionmaker, world: UUID) -> SettingsEdit:
    """Northlake's seeded settings, as the edit that puts them back."""
    async with db_sessionmaker() as session:
        policy = await session.get(RepPolicy, (world, NORTHLAKE))
        org = await session.get(Org, (world, NORTHLAKE))
        assert policy is not None
        assert org is not None
        prose = await session.get(RepSettings, (world, NORTHLAKE, org.current_settings_version))
        assert prose is not None
    return SettingsEdit(
        sharing_description=policy.sharing_description,
        autonomy=policy.autonomy,
        instructions=prose.instructions,
        public_summary=prose.public_summary,
    )


async def restore(sm: Sessionmaker, world_id: UUID, seeded: SettingsEdit) -> None:
    """Put Northlake's settings back as seeded, Henderson untrusted."""
    await change(sm, world_id, PolicyChange(seeded))
    async with unit_of_work(sm, world_id) as uow:
        row = await uow.session.get(TrustedOrganization, (world_id, NORTHLAKE, HENDERSON))
        if row is None:
            return
        await org_settings.remove_trusted(
            uow,
            org_id=NORTHLAKE,
            member_id=HELEN,
            counterparty_org_id=HENDERSON,
            version=row.version,
        )
        await uow.commit()


async def consult_result(
    sm: Sessionmaker,
    model: Checkpointed,
    records: Records,
    seat: Seat,
    *,
    thread_id: UUID,
    consult_id: UUID,
    n: int,
    plan: Answer,
) -> str:
    """Answer and settle the consult the seat's turn `n` opened, as the consult workflow
    does, with its org's person deciding when it needs one; the result the
    representative is owed."""
    world_id = records.world_id
    outcome = await answer(
        sm,
        model,
        world_id=world_id,
        consult_id=consult_id,
        cassette_thread=seat.cassette_thread,
        consult_turn=n,
    )
    # The answerer read the records the representative never sees.
    answerer_saw = canonical_json(model.requests[-1].body)
    assert all(pick(records, seat.org_id, index)[1] in answerer_saw for index in plan.reads)
    check = None
    mode = await mode_of(sm, world_id, seat)
    if isinstance(outcome, DraftReady) and release.needs_sharing_check(mode, outcome.draft):
        check = await check_sharing(
            sm,
            model,
            world_id=world_id,
            consult_id=consult_id,
            draft=outcome.draft,
            cassette_thread=seat.cassette_thread,
            consult_turn=n,
        )
        # The check sees the draft, which may copy records, and none of the org's own.
        checked = model.requests[-1]
        assert checked.key.role == "sharing_check"
        assert not MARKER.findall(DRAFT_SPAN.sub("", canonical_json(checked.body)))
    async with unit_of_work(sm, world_id) as uow:
        settled = await settle(uow, consult_id=consult_id, outcome=outcome, sharing=check)
        await uow.commit()
    owed = None
    if isinstance(settled, Escalated):
        event(f"a person decided a consult: {plan.person}")
        owed = await person_decides(sm, records, seat, thread_id, settled.escalation_id, plan)
        assert owed.content is not None
    async with sm() as session:
        row = await consults.get(session, world_id=world_id, consult_id=consult_id)
    result = settled_result(row)
    assert result is not None
    if owed is not None:
        assert result == owed.content
    return result


async def person_decides(
    sm: Sessionmaker,
    records: Records,
    seat: Seat,
    thread_id: UUID,
    escalation_id: UUID,
    plan: Answer,
) -> OwedResult:
    """The seat's org's person resolves the item as `NetworkActivities.apply_resolution`
    applies it, their text quoting one of the org's records when the plan says so; the
    result the representative is owed."""
    quoted = pick(records, seat.org_id, plan.quotes)[1] if plan.quotes is not None else None
    text = f"Per our records: {quoted}" if quoted else PERSON_TEXT[plan.person]
    applied = await NetworkActivities(cast(Client, None), sm).apply_resolution(
        ApplyResolutionInput(
            world_id=records.world_id,
            thread_id=thread_id,
            resolution=EscalationResolution(
                escalation_id=escalation_id,
                action=plan.person,
                text=text,
                member_id=DECIDER[seat.org_id],
                org_id=seat.org_id,
            ),
            message_id=uuid4(),
        )
    )
    assert applied.owed is not None
    return applied.owed


def prompt_of(body: JsonObject) -> str:
    """Everything a request holds but its messages: the frozen prompt and tools."""
    return canonical_json({key: value for key, value in body.items() if key != "messages"})


def message_texts(body: JsonObject) -> list[str]:
    """Each message block of a request as the representative reads it: a text block's
    or tool result's own string, anything else as JSON."""
    texts: list[str] = []
    for message in objects(body.get("messages")):
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        for block in objects(content):
            text = block.get("content" if block.get("type") == "tool_result" else "text")
            texts.append(text if isinstance(text, str) else canonical_json(block))
    return texts


@dataclass
class SeatState:
    """What the thread workflow tracks for one side: its turns, the results its next
    turn owes it, and the counterparty messages it has seen. Plus the oracle: the record
    bodies the plan lets those owed results carry, and the ones delivered so far."""

    turns: int = 0
    owed: list[OwedResult] = field(default_factory=list[OwedResult])
    seen_seq: int = 0
    owed_releases: set[str] = field(default_factory=set[str])
    released: set[str] = field(default_factory=set[str])


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(steps=st.lists(STEPS, min_size=2, max_size=12))
# Held messages in *Ask*: Helen's decline and edit quote a record.
@example(
    steps=[
        PolicyChange(SettingsEdit(autonomy="ask")),
        Turn(Seat(0, "responder"), "send", False, replace(PLAIN, person="decline", quotes=0)),
        Turn(Seat(0, "responder"), "text", False, replace(PLAIN, quotes=1)),
    ]
)
# A held message declined without a reason leaves nothing to answer: bare text after it
# stays private (3.13 E3).
@example(
    steps=[
        PolicyChange(SettingsEdit(autonomy="ask")),
        Turn(Seat(1, "responder"), "send", False, replace(PLAIN, person="decline")),
        Turn(Seat(1, "responder"), "text", False, PLAIN),
    ]
)
# Alex takes over Henderson's consult quoting its own record: Northlake's representative
# on that thread reads it as Henderson's words, and on the other thread never.
@example(
    steps=[
        Turn(
            Seat(0, "requester"),
            "consult",
            False,
            replace(PLAIN, needs_human=True, quotes=0, person="take_over"),
        ),
        Turn(Seat(0, "responder"), "send", False, PLAIN),
        Turn(Seat(1, "responder"), "consult", False, replace(PLAIN, reads=(0,))),
    ]
)
# A close in *Ask* isn't held: Henderson's side gets its last turn.
@example(
    steps=[
        PolicyChange(SettingsEdit(autonomy="ask")),
        Turn(Seat(0, "responder"), "close", False, PLAIN),
        Turn(Seat(1, "requester"), "close", False, PLAIN),
    ]
)
async def test_no_record_reaches_a_representative_but_inside_a_released_answer(
    db_sessionmaker: Sessionmaker, records: Records, seeded: SettingsEdit, steps: Sequence[Step]
) -> None:
    """Two threads to Northlake (from Henderson and from Ridgeline), both sides of each
    taking random turns through the `rep_turn` activity (messages, bare text,
    consults, closes, refused calls), with Northlake's settings changing in between
    (sharing, mode, instructions, summary, Henderson as a trusted organization). Every
    consult is answered by its org's answerer reading the org's records and copying
    their bodies, then released by code or decided by its person; in *Ask* every
    message is held and the person approves, edits, takes over or declines it, their
    text quoting a record at times. What a side may be given comes from the plan and
    §4.4, never from what the code returned. In every representative request, a record
    body appears only as the counterparty's own words or inside a released quote, and
    only one the plan released to that thread and side; every body it released stays in
    the side's later requests. A sharing check's request holds none outside the draft
    it judges."""
    sm, world_id = db_sessionmaker, records.world_id
    # The fixtures are shared across examples: each starts from the seeded settings, so
    # a failure replays the same way on its own.
    await restore(sm, world_id, seeded)
    model = Unfolding({})
    checkpointed = Checkpointed(model)
    opened = [
        await open_thread(sm, world_id, requester=org_id, sender=member)
        for org_id, member in REQUESTERS
    ]
    seats = {Seat(t, side): SeatState() for t in (0, 1) for side in SIDES}
    ended: set[int] = set()
    seen = consulted = 0

    async def play(seat: Seat, script: dict[CassetteKey, CassetteEntry]) -> RepTurnResult:
        nonlocal seen
        state = seats[seat]
        model.add(script)
        result = await run_rep_turn(
            sm,
            model,
            RepTurnInput(
                world_id=world_id,
                thread_id=opened[seat.thread].thread_id,
                side=seat.side,
                org_id=seat.org_id,
                cassette_thread=seat.cassette_thread,
                turn_id=uuid4(),
                turn=state.turns,
                owed=state.owed,
                deliver_after_seq=state.seen_seq,
            ),
        )
        state.released |= state.owed_releases
        state.owed, state.owed_releases = [], set()
        state.seen_seq = result.delivered_through_seq
        for request in model.requests[seen:]:
            if request.key.role != "representative":
                continue
            assert not MARKER.findall(prompt_of(request.body)), f"{seat}: a record in its prompt"
            texts = message_texts(request.body)
            for text in texts:
                for words in COUNTERPARTY_SPAN.findall(text):
                    owners = {records.owner(m) for m in MARKER.findall(words)}
                    assert owners <= {seat.counterparty_id}, f"{seat}: another org's record"
                own = COUNTERPARTY_SPAN.sub("", text)
                assert not MARKER.findall(RELEASED_SPAN.sub("", own)), f"{seat}: an unquoted record"
                assert set(MARKER.findall(own)) <= state.released, f"{seat}: a record not released"
            assert state.released <= set(MARKER.findall("".join(texts))), f"{seat}: one dropped"
        seen = len(model.requests)
        return result

    async def end(seat: Seat) -> None:
        """The seat closed its thread: the counterparty's one last turn."""
        other = Seat(seat.thread, threads.other(seat.side))
        seats[other].turns += 1
        await play(other, last_word(other, seats[other].turns))
        ended.add(seat.thread)

    for step in [*opening_turns(), *steps, *last_turns()]:
        if not isinstance(step, Turn):
            await change(sm, world_id, step)
            continue
        seat, thread = step.seat, opened[step.seat.thread]
        if seat.thread in ended:
            continue
        if step.action == "close" and not await has_written(sm, thread, seat.counterparty_id):
            # A close before any reply is refused (3.13 E1, test_e2_consult_tools): the
            # seat sends instead.
            step = replace(step, action="send")
        state = seats[seat]
        state.turns += 1
        n = state.turns
        mode = await mode_of(sm, world_id, seat)
        answers = (bool(state.owed) and not only_held_declines(state.owed)) or await has_written(
            sm, thread, seat.counterparty_id, after_seq=state.seen_seq
        )
        await to_move(sm, thread, seat.side)
        result = await play(seat, rep_script(seat, n, step))

        assert result.stop == expected_stop(step, mode, answers=answers)
        if not step.refused:
            # The executor took the call: nothing was refused for the wrong reason.
            assert [type(e) for e in result.effects] == expected_effects(
                step, mode, answers=answers
            )
        last = model.requests[-1]
        if step.refused and objects(last.body.get("messages"))[-1].get("role") == "system":
            # Its notices aren't stored (nothing may follow a system message but a
            # response), so the next request can't extend this one byte for byte.
            model.forget_conversation(last)
        effect = result.effects[0] if result.effects else None
        if isinstance(effect, ConsultOpened):
            consulted += 1
            assert result.owed is not None
            model.add(answerer_script(seat, n, step.answer, records))
            content = await consult_result(
                sm,
                checkpointed,
                records,
                seat,
                thread_id=thread.thread_id,
                consult_id=effect.consult_id,
                n=n,
                plan=step.answer,
            )
            state.owed = [OwedResult(tool_use_id=result.owed.tool_use_id, content=content)]
            state.owed_releases = consult_releases(records, seat, step.answer, mode)
        elif isinstance(effect, MessageHeld):
            event(f"a person decided a held message: {step.answer.person}")
            held = await person_decides(
                sm, records, seat, thread.thread_id, effect.escalation_id, step.answer
            )
            # A bare-text reply made no call: its result is owed to none.
            call = result.owed.tool_use_id if result.owed is not None else ""
            state.owed = [held.model_copy(update={"tool_use_id": call})]
            state.owed_releases = person_quotes(records, seat, step.answer)
        elif isinstance(effect, MessageSent) and effect.message_kind == "close":
            await end(seat)
    event(f"threads closed: {len(ended)}")
    event(f"consults: {consulted}")
