"""The beats a story run records: a step's `expect` as atoms, and the run's result.

Beats are matched as atoms, so `closed: [northlake, ridgeline]` is two facts that can
arrive in either order. `recast` plays a story as another member of its org, and
`free_form` makes a story of one message with no beats (`harmony run --message`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from harmony.seeds.schema import (
    Beat,
    Closed,
    InlineQuestion,
    OutreachApproved,
    Resolved,
    Routed,
    StorySpec,
    StoryStepSpec,
)

type Atom = tuple[str, ...]
# `ended`: the request closed, or a thread the story needs expired, without the
# remaining beats.
type Outcome = Literal["done", "ended", "timeout", "failed"]


def atoms(beat: Beat) -> frozenset[Atom]:
    """The facts a step's `expect` stands for."""
    match beat:
        case OutreachApproved():
            return frozenset({("outreach_approved",)})
        case InlineQuestion(org=org, member=member):
            return frozenset({("inline_question", org, member)})
        case Routed(org=org, member=member):
            return frozenset({("routed", org, member)})
        case Resolved(org=org, member=member, action=action):
            return frozenset({("resolved", org, member, action)})
        case Closed(orgs=orgs):
            return frozenset(("closed", org) for org in orgs)


def describe(atom: Atom) -> str:
    match atom:
        case ("outreach_approved",):
            return "outreach approved"
        case ("inline_question", org, member):
            return f"{org}'s question reached {member} in the chat"
        case ("routed", org, member):
            return f"{org} routed an escalation to {member}"
        case ("resolved", org, member, action):
            return f"{member} resolved {org}'s escalation: {action}"
        case ("closed", org):
            return f"the conversation with {org} closed"
        case _:
            return " ".join(atom)


@dataclass(frozen=True, slots=True)
class SeenBeat:
    atom: Atom
    elapsed_s: float
    # Wall time, to line the beat up with what the database recorded meanwhile.
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class StoryRun:
    world_id: UUID
    expected: list[tuple[StoryStepSpec, frozenset[Atom]]]
    conversation_id: UUID | None = None
    # Every request the chat sent, in the order the run saw them.
    request_ids: list[UUID] = field(default_factory=list[UUID])
    # Each request's thread → its responder org's slug.
    threads: dict[UUID, str] = field(default_factory=dict[UUID, str])
    seen: list[SeenBeat] = field(default_factory=list[SeenBeat])
    outcome: Outcome = "failed"
    detail: str | None = None

    @property
    def request_id(self) -> UUID | None:
        """The chat's first request: the one a story is about."""
        return self.request_ids[0] if self.request_ids else None

    def add_request(self, request_id: UUID | None) -> None:
        if request_id is not None and request_id not in self.request_ids:
            self.request_ids.append(request_id)

    @property
    def seen_atoms(self) -> set[Atom]:
        return {beat.atom for beat in self.seen}

    @property
    def complete(self) -> bool:
        return all(wanted <= self.seen_atoms for _, wanted in self.expected)

    def missing(self) -> list[StoryStepSpec]:
        return [step for step, wanted in self.expected if not wanted <= self.seen_atoms]

    def missing_atoms(self) -> set[Atom]:
        return {atom for _, wanted in self.expected for atom in wanted} - self.seen_atoms


def expected_beats(story: StorySpec) -> list[tuple[StoryStepSpec, frozenset[Atom]]]:
    return [(step, atoms(step.expect)) for step in story.steps if step.expect is not None]


def free_form(member: str, message: str) -> StorySpec:
    """A story of `member` sending `message`, with no beats and no rules: the run
    follows whatever requests come of it to their end."""
    return StorySpec.model_validate({"name": "message", "as": member, "suggested_message": message})


def recast(story: StorySpec, member: str) -> StorySpec:
    """`story` with `member` in its member's part: sending the message, and every step,
    beat and rule that names the story's member. Others keep theirs."""
    cast = story.member
    if member == cast:
        return story

    def swap(slug: str) -> str:
        return member if slug == cast else slug

    def step(spec: StoryStepSpec) -> StoryStepSpec:
        expect = spec.expect
        if isinstance(expect, InlineQuestion | Routed | Resolved):
            expect = expect.model_copy(update={"member": swap(expect.member)})
        act_as = swap(spec.act_as) if spec.act_as is not None else None
        return spec.model_copy(update={"act_as": act_as, "expect": expect})

    return story.model_copy(
        update={
            "member": member,
            "steps": [step(spec) for spec in story.steps],
            "humans": [
                rule.model_copy(update={"member": swap(rule.member)}) for rule in story.humans
            ],
        }
    )
