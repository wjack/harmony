"""Demo route: what the walkthrough suggests next (§13, UI_REFERENCE §9).

- GET /api/demo/story: the scenario's story beat for the acting member
- GET /api/demo/whose?chat=<id> or ?thread=<id>: who in the world can open a chat or
  thread the acting member can't, so the not-found page can switch to one of them.
  Demo chrome (§11.4): names and orgs only, never a title, content or state, and only
  for ids in the session's own live world; any other id is a 404 like an unknown one
- GET /api/demo/waiting: every account in the world with an open item assigned to it,
  and how many, for the demo bar's Network view badge. Demo chrome: who and how many,
  never what

Read from the world's scenario (`scenarios/<name>/story.yaml`, E5): the suggested
opening message for the composer (for the story's sender), the recipients the story
expects (for the sender's org, whose request it is), the world's date, and the
walkthrough steps. Payload-free `demo.notice` / `demo.needs_human` events move
the walkthrough along; this route is what it reads.

Owner: E4, reading E5's `story.yaml` (U1 builds the demo bar and walkthrough).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date
from functools import cache
from typing import Final, Literal
from uuid import UUID

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, exists, or_
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents.models import MemberRef
from harmony.api.deps import SessionmakerDep
from harmony.api.routes.problems import invalid, member_ref, not_found, problems
from harmony.auth.session import Actor, ActorDep
from harmony.auth.views import OrgRef, member_rows, org_ref
from harmony.db import repo
from harmony.db.models import Conversation, Escalation, Member, Org, Thread, ThreadAccess, World
from harmony.domain import clock
from harmony.seeds.dates import render_dates
from harmony.seeds.loader import ScenarioNotFound, load_scenario
from harmony.seeds.schema import ScenarioError, StorySpec

router = APIRouter(tags=["demo"])

# The most people the not-found page names, across both orgs of a thread.
WHOSE_MAX_PEOPLE: Final = 3


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class StoryStep(_View):
    id: str
    title: str
    body: str
    # Who to act as for this step (act-as), if it matters.
    act_as: MemberRef | None
    # The `demo.notice` / `demo.needs_human` kind that completes the step.
    done_on: str | None


class DemoStory(_View):
    scenario: str
    world_date: date
    acting_as: MemberRef
    suggested_message: str | None
    suggested_recipients: list[OrgRef]
    steps: list[StoryStep]


class DemoPerson(_View):
    member_id: UUID
    name: str


class DemoWhoseOrg(_View):
    org: OrgRef
    people: list[DemoPerson]


class DemoWhose(_View):
    """Names and orgs only: never a title, content or state."""

    kind: Literal["chat", "thread"]
    # A chat: its owner's org. A thread: the requester's org, then the responder's.
    orgs: list[DemoWhoseOrg]


@router.get("/demo/story", response_model=DemoStory, responses=problems())
async def get_story(actor: ActorDep, sessionmaker: SessionmakerDep) -> DemoStory:
    async with sessionmaker() as db:
        world = await db.get(World, actor.world_id)
        acting_as = await member_ref(db, actor.world_id, actor.member_id)
        if world is None or acting_as is None:
            raise not_found("world")
        today = await clock.today(db, actor.world_id)
        story = scenario_story(world.scenario)
        if story is None:
            return DemoStory(
                scenario=world.scenario,
                world_date=today,
                acting_as=acting_as,
                suggested_message=None,
                suggested_recipients=[],
                steps=[],
            )
        members = await _members_by_slug(db, actor, story)
        sender = members.get(story.member)
        sender_row = (
            None if sender is None else await repo.get(db, Member, actor.world_id, sender.id)
        )
        # The story's picks are for the side that sends its request, so no other org's
        # members see them suggested.
        recipients = (
            await suggested_orgs(db, actor.world_id, story)
            if sender_row is not None and sender_row.org_id == actor.org_id
            else []
        )
    return DemoStory(
        scenario=world.scenario,
        world_date=today,
        acting_as=acting_as,
        # The composer's suggestion is for the member the story starts as.
        suggested_message=(
            render_dates(story.suggested_message, today)
            if sender is not None and sender.id == actor.member_id
            else None
        ),
        suggested_recipients=recipients,
        steps=[
            StoryStep(
                id=step.id,
                title=render_dates(step.title, today),
                body=render_dates(step.body, today),
                act_as=members.get(step.act_as) if step.act_as is not None else None,
                done_on=step.done_on,
            )
            for step in story.steps
        ],
    )


@router.get("/demo/whose", response_model=DemoWhose, responses=problems(status.HTTP_404_NOT_FOUND))
async def get_whose(
    actor: ActorDep,
    sessionmaker: SessionmakerDep,
    chat: UUID | None = None,
    thread: UUID | None = None,
) -> DemoWhose:
    """Exactly one of `chat` and `thread` (422 otherwise); 404 for an id that isn't in
    the acting member's world."""
    async with sessionmaker() as db:
        if chat is not None and thread is None:
            return DemoWhose(kind="chat", orgs=await _chat_openers(db, actor.world_id, chat))
        if thread is not None and chat is None:
            return DemoWhose(kind="thread", orgs=await _thread_openers(db, actor.world_id, thread))
    raise invalid("give exactly one of chat and thread")


class DemoWaitingAccount(_View):
    member: MemberRef
    org: OrgRef
    # Open items assigned to this member.
    items: int


class DemoWaiting(_View):
    accounts: list[DemoWaitingAccount]


@router.get("/demo/waiting", response_model=DemoWaiting, responses=problems())
async def get_waiting(actor: ActorDep, sessionmaker: SessionmakerDep) -> DemoWaiting:
    """Every account with an open item assigned to it, the acting member's included."""
    async with sessionmaker() as db:
        counts: Counter[UUID] = Counter()
        for escalation in await db.scalars(
            repo.in_world(Escalation, actor.world_id).where(Escalation.status == "open")
        ):
            counts.update(UUID(str(member_id)) for member_id in escalation.assignees)
        rows = await member_rows(db, actor.world_id, Member.id.in_(list(counts)))
    return DemoWaiting(
        accounts=[
            DemoWaitingAccount(
                member=MemberRef(id=member.id, slug=member.slug, name=member.name),
                org=org_ref(org),
                items=counts[member.id],
            )
            for member, org in rows
        ]
    )


@cache
def scenario_story(scenario: str) -> StorySpec | None:
    """The scenario's story, or None (no story, or a world without a scenario)."""
    try:
        return load_scenario(scenario).story
    except (ScenarioNotFound, ScenarioError):
        return None


async def suggested_orgs(db: AsyncSession, world_id: UUID, story: StorySpec) -> list[OrgRef]:
    """The story's suggested recipients present in the world, in the story's order."""
    if not story.suggested_recipients:
        return []
    rows = {
        org.slug: org
        for org in await db.scalars(
            repo.in_world(Org, world_id).where(Org.slug.in_(story.suggested_recipients))
        )
    }
    return [org_ref(rows[slug]) for slug in story.suggested_recipients if slug in rows]


async def _chat_openers(db: AsyncSession, world_id: UUID, chat_id: UUID) -> list[DemoWhoseOrg]:
    """A chat's org, with its owner: only the owner can open a chat."""
    conversation = await repo.get(db, Conversation, world_id, chat_id)
    if conversation is None:
        raise not_found("chat")
    owner = Member.id == conversation.owner_member_id
    return await _openers(db, world_id, [conversation.org_id], owner)


async def _thread_openers(db: AsyncSession, world_id: UUID, thread_id: UUID) -> list[DemoWhoseOrg]:
    """A thread's requester org, then its responder org, each with the members who can
    open it by `auth.access`'s rule: its admins, and anyone with a `thread_access` row."""
    thread = await repo.get(db, Thread, world_id, thread_id)
    if thread is None:
        raise not_found("thread")
    has_access = exists().where(
        ThreadAccess.world_id == Member.world_id,
        ThreadAccess.thread_id == thread.id,
        ThreadAccess.member_id == Member.id,
    )
    parties = [thread.requester_org_id, thread.responder_org_id]
    return await _openers(db, world_id, parties, or_(Member.role == "admin", has_access))


async def _openers(
    db: AsyncSession, world_id: UUID, org_ids: Sequence[UUID], can_open: ColumnElement[bool]
) -> list[DemoWhoseOrg]:
    """Each of `org_ids`, in order, with its members who `can_open` the item, in member
    order: at most `WHOSE_MAX_PEOPLE` in all, and at least one per org that has any."""
    orgs = [await repo.get_required(db, Org, world_id, org_id) for org_id in org_ids]
    rows = await member_rows(db, world_id, Member.org_id.in_(org_ids), can_open)
    groups = [[member for member, org in rows if org.id == each.id] for each in orgs]
    left = WHOSE_MAX_PEOPLE
    result: list[DemoWhoseOrg] = []
    for index, (org, group) in enumerate(zip(orgs, groups, strict=True)):
        kept_for_later = sum(1 for later in groups[index + 1 :] if later)
        people = group[: max(0, left - kept_for_later)]
        left -= len(people)
        result.append(
            DemoWhoseOrg(
                org=org_ref(org),
                people=[DemoPerson(member_id=member.id, name=member.name) for member in people],
            )
        )
    return result


async def _members_by_slug(
    db: AsyncSession, actor: Actor, story: StorySpec
) -> dict[str, MemberRef]:
    slugs = {story.member, *(step.act_as for step in story.steps if step.act_as is not None)}
    rows = await db.scalars(repo.in_world(Member, actor.world_id).where(Member.slug.in_(slugs)))
    return {m.slug: MemberRef(id=m.id, slug=m.slug, name=m.name) for m in rows}
