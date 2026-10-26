"""Typed event payloads: the durable outbox events and the ephemeral stream events.

Two families share one discriminated union, keyed on `type`:

- **Durable events** are written to the `events` table through the UnitOfWork
  (`harmony.db.uow`). Each gets a per-world `seq`, which is also its SSE `id`.
- **Stream events** (`stream.*`) are never persisted. They travel over the bus
  (`harmony.events.bus.StreamPublisher`) and carry no `seq`, so they never move a
  client's `Last-Event-ID`.

What one SSE frame carries is an `EventEnvelope`. Framing (implemented in
`events/sse.py`): durable events are sent as `id: <seq>` + `data: <envelope JSON>`;
stream events as `data:` only; no `event:` field, so `EventSource.onmessage` sees
every frame; heartbeats are SSE comments.

Add new event types here (one model + one union member), never as free-form dicts:
the union is exported into the OpenAPI document so the web client is typed end to end.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

ScopeKind = Literal["world", "org", "member", "thread", "demo", "session"]
WorldMode = Literal["live", "replay"]
AccessReason = Literal["routed", "shared", "admin"]

# `EntitySnapshot.entity_type` values. Every `Snapshotable` model's type
# (`db.models.SNAPSHOT_ENTITIES`), plus the append-only rows in
# `APPEND_ONLY_ENTITY_TYPES`, which are never updated and so are snapshotted once at
# version 1 through `UnitOfWork.add_event` (as P0's turn does for `thread_message`).
# Both sets are checked by tests/unit/test_contract_routes.py. `entity_type` stays `str`
# on the wire so an older client ignores a newer type.
EntityType = Literal[
    "org",
    "member",
    "rep_settings",
    "rep_policy",
    "trusted_organization",
    "conversation",
    "card",
    "network_request",
    "thread",
    "consult",
    "escalation",
    "knowledge_item",
    "document",
    "thread_message",
    "internal_note",
    "escalation_draft",
]
ENTITY_TYPES: tuple[EntityType, ...] = (
    "org",
    "member",
    "rep_settings",
    "rep_policy",
    "trusted_organization",
    "conversation",
    "card",
    "network_request",
    "thread",
    "consult",
    "escalation",
    "knowledge_item",
    "document",
    "thread_message",
    "internal_note",
    "escalation_draft",
)
APPEND_ONLY_ENTITY_TYPES: tuple[EntityType, ...] = (
    "thread_message",
    "internal_note",
    "escalation_draft",
)


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- Scope --------------------------------------------------------------------------


class EventScope(_Payload):
    """Who may receive an event. Stored in the `events` row's scope columns.

    Delivery rules (enforced by `events/sse.py`; the network lens sees everything):
    - `world`: every member of the world.
    - `org`: members of `org_id`.
    - `thread`: members of `org_id` with access to `thread_id` (routed, shared, admin).
    - `member`: only `member_id`.
    - `demo`: everyone; the payload must be a `DemoNotice` (no entity data).
    - `session`: only streams opened by `session_id`.
    """

    kind: ScopeKind
    org_id: UUID | None = None
    member_id: UUID | None = None
    thread_id: UUID | None = None
    session_id: UUID | None = None

    @model_validator(mode="after")
    def _required_ids(self) -> Self:
        required: dict[ScopeKind, tuple[str, ...]] = {
            "world": (),
            "demo": (),
            "org": ("org_id",),
            "thread": ("org_id", "thread_id"),
            "member": ("member_id",),
            "session": ("session_id",),
        }
        missing = [name for name in required[self.kind] if getattr(self, name) is None]
        if missing:
            raise ValueError(f"{self.kind} scope requires {', '.join(missing)}")
        return self

    @classmethod
    def world(cls) -> EventScope:
        return cls(kind="world")

    @classmethod
    def demo(cls) -> EventScope:
        return cls(kind="demo")

    @classmethod
    def org(cls, org_id: UUID) -> EventScope:
        return cls(kind="org", org_id=org_id)

    @classmethod
    def thread(cls, org_id: UUID, thread_id: UUID) -> EventScope:
        return cls(kind="thread", org_id=org_id, thread_id=thread_id)

    @classmethod
    def member(cls, member_id: UUID, org_id: UUID | None = None) -> EventScope:
        return cls(kind="member", member_id=member_id, org_id=org_id)

    @classmethod
    def session(cls, session_id: UUID) -> EventScope:
        return cls(kind="session", session_id=session_id)


# --- Durable events -----------------------------------------------------------------


class WorldCreated(_Payload):
    type: Literal["world.created"] = "world.created"
    scenario: str
    mode: WorldMode
    build_sha: str


class WorldRetired(_Payload):
    type: Literal["world.retired"] = "world.retired"


class SessionMemberChanged(_Payload):
    """Act-as switched the acting member. Session-scoped; streams on an older epoch close."""

    type: Literal["session.member_changed"] = "session.member_changed"
    session_id: UUID
    member_id: UUID
    epoch: int


class ThreadAccessChanged(_Payload):
    type: Literal["thread.access_changed"] = "thread.access_changed"
    thread_id: UUID
    member_id: UUID
    reason: AccessReason
    # True when the member gained this access reason, False when they lost it.
    added: bool
    shared_by: UUID | None = None
    question: str | None = None


class EntitySnapshot(_Payload):
    """The full current state of one entity. Clients apply it only if `version` is newer.

    `entity_type`, `entity_id` and `version` are also written to the `events` row's
    entity columns so snapshots can be replayed into a fresh world.
    """

    type: Literal["entity.snapshot"] = "entity.snapshot"
    entity_type: str
    entity_id: UUID
    version: int = Field(ge=1)
    data: dict[str, JsonValue]


class DemoNotice(_Payload):
    """A payload-free cue for the demo bar and walkthrough. Never touches the query cache.

    `kind` names what happened (for example `escalation.created`); the client fetches
    details itself, under the acting member's access, if it needs them.
    """

    type: Literal["demo.notice"] = "demo.notice"
    kind: str
    org_slug: str | None = None
    entity_id: UUID | None = None


class DemoNeedsHuman(DemoNotice):
    """The demo bar's "needs a human" cue: an escalation opened for `member_slug` at
    `org_slug` (§13). Still payload-free: `entity_id` is the escalation's id, which the
    client fetches under the acting member's access.

    A `DemoNotice` subclass, so the demo scope accepts it (`db/uow.py`, `events/sse.py`).
    """

    type: Literal["demo.needs_human"] = "demo.needs_human"  # pyright: ignore[reportIncompatibleVariableOverride]
    kind: str = "needs_human"
    member_slug: str | None = None

    @model_validator(mode="after")
    def _names_who(self) -> Self:
        if self.org_slug is None or self.member_slug is None or self.entity_id is None:
            raise ValueError("demo.needs_human needs org_slug, member_slug and entity_id")
        return self


class RedraftFailed(_Payload):
    """A redraft ("Ask Claude to redraft") ended without writing `draft_rev`: the
    answerer's call failed or returned no draft. Scoped like its escalation, so the
    members looking at the item stop waiting for that rev and can ask again."""

    type: Literal["escalation.redraft_failed"] = "escalation.redraft_failed"
    escalation_id: UUID
    draft_rev: int = Field(ge=1)


class HelloTick(_Payload):
    """Emitted by the P0 `HelloWorkflow` to prove the worker → outbox → SSE path."""

    type: Literal["hello.tick"] = "hello.tick"
    workflow_id: str
    n: int = Field(ge=1)
    of: int = Field(ge=1)


# --- Stream events (ephemeral, never persisted) -------------------------------------


class StreamDelta(_Payload):
    """Coalesced model output (~200 ms). Sent only to the producing org's members with
    thread access, and to the network lens."""

    type: Literal["stream.delta"] = "stream.delta"
    org_id: UUID
    thread_id: UUID
    turn_id: UUID
    attempt: int = Field(ge=1)
    text: str


class StreamTyping(_Payload):
    """What everyone else in the thread sees while `org_id`'s representative writes."""

    type: Literal["stream.typing"] = "stream.typing"
    org_id: UUID
    thread_id: UUID
    turn_id: UUID


class StreamReset(_Payload):
    """A retried attempt began: discard partial text from earlier attempts of the turn."""

    type: Literal["stream.reset"] = "stream.reset"
    org_id: UUID
    thread_id: UUID
    turn_id: UUID
    attempt: int = Field(ge=1)


class StreamChatDelta(_Payload):
    """Coalesced internal Claude output in a conversation (P1, §5, §12). Sent only to
    the conversation's owner (`member_id`) and the network lens (`events/sse.py`
    routes it by member, with no thread check: `StreamDelta` requires a thread).

    A higher `attempt` for the same `turn_id` means a retried attempt began: the client
    discards the earlier attempts' text (there is no reset event).
    """

    type: Literal["stream.chat_delta"] = "stream.chat_delta"
    org_id: UUID
    member_id: UUID
    conversation_id: UUID
    turn_id: UUID
    attempt: int = Field(ge=1)
    text: str


DurableEvent = Annotated[
    WorldCreated
    | WorldRetired
    | SessionMemberChanged
    | ThreadAccessChanged
    | EntitySnapshot
    | DemoNotice
    | DemoNeedsHuman
    | RedraftFailed
    | HelloTick,
    Field(discriminator="type"),
]

# The stream events that belong to a thread (routed by thread access).
ThreadStreamEvent = StreamDelta | StreamTyping | StreamReset

StreamEvent = Annotated[
    StreamDelta | StreamTyping | StreamReset | StreamChatDelta,
    Field(discriminator="type"),
]

HarmonyEvent = Annotated[
    WorldCreated
    | WorldRetired
    | SessionMemberChanged
    | ThreadAccessChanged
    | EntitySnapshot
    | DemoNotice
    | DemoNeedsHuman
    | RedraftFailed
    | HelloTick
    | StreamDelta
    | StreamTyping
    | StreamReset
    | StreamChatDelta,
    Field(discriminator="type"),
]

EventType = Literal[
    "world.created",
    "world.retired",
    "session.member_changed",
    "thread.access_changed",
    "entity.snapshot",
    "demo.notice",
    "demo.needs_human",
    "escalation.redraft_failed",
    "hello.tick",
    "stream.delta",
    "stream.typing",
    "stream.reset",
    "stream.chat_delta",
]


class EventEnvelope(_Payload):
    """One SSE frame's `data`. `seq` and `created_at` are None for stream events."""

    world_id: UUID
    seq: int | None
    created_at: datetime | None
    scope: EventScope
    event: HarmonyEvent
