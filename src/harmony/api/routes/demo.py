"""Demo route: what the walkthrough suggests next (§13, UI_REFERENCE §9).

- GET /api/demo/story: the scenario's story beat for the acting member

Read from the world's scenario (`scenarios/<name>/story.yaml`, E5): the suggested
opening message for the composer (for the story's sender), the recipients the story
expects (for the sender's org, whose request it is), the world's date, and the
walkthrough steps. Payload-free `demo.notice` / `demo.needs_human` events move
the walkthrough along; this route is what it reads.

Owner: E4, reading E5's `story.yaml` (U1 builds the demo bar and walkthrough).
"""

from __future__ import annotations

from datetime import date
from functools import cache
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.agents.models import MemberRef
from harmony.api.deps import SessionmakerDep
from harmony.api.routes.problems import member_ref, not_found, problems
from harmony.auth.session import Actor, ActorDep
from harmony.auth.views import OrgRef, org_ref
from harmony.db import repo
from harmony.db.models import Member, Org, World
from harmony.domain import clock
from harmony.seeds.dates import render_dates
from harmony.seeds.loader import ScenarioNotFound, load_scenario
from harmony.seeds.schema import ScenarioError, StorySpec

router = APIRouter(tags=["demo"])


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


async def _members_by_slug(
    db: AsyncSession, actor: Actor, story: StorySpec
) -> dict[str, MemberRef]:
    slugs = {story.member, *(step.act_as for step in story.steps if step.act_as is not None)}
    rows = await db.scalars(repo.in_world(Member, actor.world_id).where(Member.slug.in_(slugs)))
    return {m.slug: MemberRef(id=m.id, slug=m.slug, name=m.name) for m in rows}
