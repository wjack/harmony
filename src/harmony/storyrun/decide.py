"""What `harmony run` does where a person would act.

`StoryDecisions` follows the story: approve the outreach to the orgs its beats expect
to hear from (leaving out a near miss such as Calder), and resolve each escalation by
the next `humans` rule that matches it (else approve the draft as written).
`ApproveAll` (`--approve all`) approves every listed recipient and each escalation's
draft as written. `PromptDecisions` (`--interactive`) asks at each of those points,
with the story's choice as the default.

Approvals always name their recipients, as the web does: the server's default applies
only to an approval that names none.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, get_args
from uuid import UUID

from harmony.agents.models import CardRecipient, EscalationAction, MemberRef, OutreachCardItem
from harmony.api.routes.cards import ApproveCard, RejectCard
from harmony.api.routes.escalations import ResolveEscalation
from harmony.db.models.choices import EscalationKind
from harmony.seeds.schema import Closed, HumanRule, InlineQuestion, Resolved, Routed, StorySpec

# A consult with no usable draft and no rule text: nothing to approve, so decline.
NO_DRAFT_TEXT = "Sorry, we can't help with this one."


@dataclass(frozen=True, slots=True)
class PendingEscalation:
    """An open escalation, as the member resolving it sees it."""

    escalation_id: UUID
    kind: EscalationKind
    # The deciding org and the member acting for it.
    org: str
    member: MemberRef
    counterparty: str
    thread_id: UUID
    question: str
    # "Why this needs you".
    reason: str | None
    # The current draft: a consult's drafted answer, or a held message.
    draft: str | None
    draft_rev: int | None


type CardChoice = ApproveCard | RejectCard


class Decisions(Protocol):
    async def outreach(self, card: OutreachCardItem) -> CardChoice: ...

    async def escalation(self, pending: PendingEscalation) -> ResolveEscalation: ...


def matches(rule: HumanRule, pending: PendingEscalation) -> bool:
    return (rule.org, rule.kind) == (pending.org, pending.kind) and rule.counterparty in (
        None,
        pending.counterparty,
    )


def expected_orgs(story: StorySpec) -> set[str]:
    """The orgs the story's beats name: the ones it expects to hear from."""
    orgs: set[str] = set()
    for step in story.steps:
        match step.expect:
            case Closed(orgs=closed):
                orgs.update(closed)
            case InlineQuestion(org=org) | Routed(org=org) | Resolved(org=org):
                orgs.add(org)
            case _:
                pass
    return orgs


def story_recipients(story: StorySpec, card: OutreachCardItem) -> ApproveCard:
    """The card approved for the recipients the story expects to hear from, or for the
    ones it proposes when the story expects none of them. The story decides rather
    than the card's fits, which say how each org was screened, not what the story
    needs."""
    expected = expected_orgs(story)
    kept = [r.org.id for r in card.recipients if r.org.slug in expected]
    return ApproveCard(recipients=kept or [r.org.id for r in proposed(card.recipients)])


def proposed(recipients: Sequence[CardRecipient]) -> list[CardRecipient]:
    """The rows the card ticks by default: suggested ones, else those whose fit isn't
    `no` (a card from before suggestions), else all (`requests.default_recipients`)."""
    return (
        [r for r in recipients if r.suggested]
        or [r for r in recipients if r.fit != "no"]
        or list(recipients)
    )


def as_drafted(pending: PendingEscalation) -> ResolveEscalation:
    """The draft approved as written; a consult with no draft is declined, and a turn
    Claude declined ends the conversation, as it did before a person could retry it."""
    if pending.kind == "refused_turn":
        return ResolveEscalation(action="decline")
    if pending.kind == "consult" and not pending.draft:
        return ResolveEscalation(action="decline", text=NO_DRAFT_TEXT)
    return ResolveEscalation(action="approve", draft_rev=pending.draft_rev)


class ApproveAll:
    """Every listed recipient, every draft as written: a free-form request run to its
    end (`--approve all`)."""

    async def outreach(self, card: OutreachCardItem) -> CardChoice:
        return ApproveCard(recipients=[r.org.id for r in card.recipients])

    async def escalation(self, pending: PendingEscalation) -> ResolveEscalation:
        return as_drafted(pending)


class StoryDecisions:
    """Rules are used in order, each for one escalation, since one org and counterparty
    may face several decisions (a question, then a go-ahead). An escalation asked again,
    such as one another assignee takes over, keeps the rule it was given."""

    def __init__(self, story: StorySpec) -> None:
        self._story = story
        self._rules = list(story.humans)
        self._used: set[int] = set()
        self._given: dict[UUID, HumanRule | None] = {}

    def rule_for(self, pending: PendingEscalation) -> HumanRule | None:
        if pending.escalation_id in self._given:
            return self._given[pending.escalation_id]
        index = next(
            (
                i
                for i, rule in enumerate(self._rules)
                if i not in self._used and matches(rule, pending)
            ),
            None,
        )
        rule = None
        if index is not None:
            self._used.add(index)
            rule = self._rules[index]
        self._given[pending.escalation_id] = rule
        return rule

    async def outreach(self, card: OutreachCardItem) -> CardChoice:
        return story_recipients(self._story, card)

    async def escalation(self, pending: PendingEscalation) -> ResolveEscalation:
        rule = self.rule_for(pending)
        if rule is None or (rule.action == "approve" and rule.text is None):
            return as_drafted(pending)
        return ResolveEscalation(action=rule.action, text=rule.text, draft_rev=pending.draft_rev)


type Ask = Callable[[str, str], str]


def _ask_terminal(prompt: str, default: str) -> str:
    import typer

    return str(typer.prompt(prompt, default=default, show_default=bool(default)))


class PromptDecisions:
    """Asks on the terminal, off the event loop, so the worker keeps serving meanwhile.
    Enter takes the story's choice."""

    def __init__(
        self,
        story: StorySpec,
        *,
        ask: Ask = _ask_terminal,
        show: Callable[[str], None] = print,
    ) -> None:
        self._spec = story
        self._story = StoryDecisions(story)
        self._ask = ask
        self._show = show

    async def outreach(self, card: OutreachCardItem) -> CardChoice:
        default = story_recipients(self._spec, card)
        chosen = default.recipients
        self._show("\nClaude proposes this request:")
        self._show("\n".join(f"  {line}" for line in card.brief.splitlines()))
        for recipient in card.recipients:
            left_out = chosen is not None and recipient.org.id not in chosen
            self._show(
                f"  - {recipient.org.name} ({recipient.fit or 'unscreened'})"
                + (", left out" if left_out else "")
                + f": {recipient.reason}"
            )
        answer = await self._prompt("Send it? Enter to approve, or type feedback for Claude", "")
        return RejectCard(feedback=answer) if answer.strip() else default

    async def escalation(self, pending: PendingEscalation) -> ResolveEscalation:
        default = await self._story.escalation(pending)
        self._show(
            f"\n{pending.member.name} ({pending.org}) is asked, for {pending.counterparty}:\n"
            f"  {pending.question}"
        )
        if pending.reason:
            self._show(f"  Why this needs you: {pending.reason}")
        if pending.draft is not None:
            self._show(f"  Claude's draft: {pending.draft}")
        actions = get_args(EscalationAction)
        action = await self._choose("Action", actions, default.action)
        text = await self._prompt("Text (Enter keeps the draft)", default.text or "")
        return ResolveEscalation(action=action, text=text or None, draft_rev=default.draft_rev)

    async def _prompt(self, prompt: str, default: str) -> str:
        return await asyncio.to_thread(self._ask, prompt, default)

    async def _choose[T: str](self, prompt: str, options: Sequence[T], default: T) -> T:
        while True:
            answer = await self._prompt(f"{prompt} ({'/'.join(options)})", default)
            for option in options:
                if option == answer.strip():
                    return option
            self._show(f"  choose one of: {', '.join(options)}")
