"""The scenario YAML format (ARCHITECTURE.md §11.7), as strict Pydantic models.

A scenario is a directory under `scenarios/`:

    <name>/scenario.yaml     name, anchor, default member, communities, org order
    <name>/orgs/<slug>.yaml  one org: members, knowledge, settings and policy
    <name>/threads.yaml      optional: seeded threads, their messages and access, and
                             open requests the world starts with
    <name>/story.yaml        optional: the suggested story (§11.5)

Every model forbids unknown fields and coerces nothing, so a typo fails at load time
with its file and YAML path instead of silently seeding a default. References between
files (member, knowledge and org slugs) are checked by `check_references`, which
reports problems with the same precision.

Dates are absolute or relative to the anchor (`at: -30d`). Everything an agent reads
(summaries, instructions, sharing descriptions, briefs) is prose
(REVISION_UNSTRUCTURED.md §1).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainValidator,
    StringConstraints,
    ValidationError,
)

from harmony.db.models.choices import (
    AccessReason,
    Autonomy,
    DirectoryVisibility,
    EscalationKind,
    Fit,
    MemberRole,
    Provenance,
    Side,
    ThreadMessageKind,
    ThreadState,
)
from harmony.seeds.dates import date_templates, is_relative_date, resolve_date

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"

Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN, max_length=64)]
# A required, non-blank string; surrounding whitespace (YAML block scalars) is trimmed.
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Prose = Annotated[str, StringConstraints(strip_whitespace=True)]


def _relative_date(value: object) -> date | str:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and is_relative_date(value):
        return value
    raise ValueError("expected a date such as 2026-11-06 or a relative date such as +11d")


RelativeDate = Annotated[date | str, PlainValidator(_relative_date)]


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


# --- scenario.yaml --------------------------------------------------------------------


class CommunitySpec(_Strict):
    slug: Slug
    name: Text
    kind: Text


class ScenarioSpec(_Strict):
    name: Slug
    # Scenario day zero: relative dates resolve against it (§11.1).
    anchor: date
    # The member a new session acts as.
    default_member: Slug
    # The org files to load, in order; each is orgs/<slug>.yaml.
    orgs: list[Slug] = Field(min_length=1)
    communities: list[CommunitySpec] = []


# --- orgs/<slug>.yaml -----------------------------------------------------------------


class MemberSpec(_Strict):
    slug: Slug
    name: Text
    title: Text | None = None
    role: MemberRole
    # What they handle, in prose; the answerer reads it when routing (§6).
    profile: Prose = ""


class KnowledgeSpec(_Strict):
    """One of the org's own records. Only internal Claude's tools read it (§2)."""

    slug: Slug
    title: Text
    # price_list, schedule, note, ... (free text)
    kind: Slug = "note"
    body: Text
    # The member who wrote it, if a member did.
    author: Slug | None = None


class SettingsSpec(_Strict):
    """Version 1 of the org's prompt-facing settings."""

    # The public summary: what counterparties and the discovery screener read.
    public_summary: Text
    # The representative's private prompt, in prose.
    instructions: Text


class TrustedOrgSpec(_Strict):
    """A counterparty the org treats differently (§3)."""

    org: Slug
    # None: the org's default mode.
    autonomy: Autonomy | None = None
    # "Also share with them: ..."
    sharing_description: Prose = ""


class PolicySpec(_Strict):
    """The org's live policy (§3, §4, §6)."""

    autonomy: Autonomy = "auto"
    # What the representative may pass on to any counterparty, in prose.
    sharing_description: Prose = ""
    routing_prompt: Text
    default_assignee: Slug
    directory_visibility: DirectoryVisibility = "public"


class OrgSpec(_Strict):
    slug: Slug
    name: Text
    # The network view's category: research, characterization, fabrication, ...
    kind: Slug
    featured: bool = False
    communities: list[Slug] = []
    members: list[MemberSpec] = Field(min_length=1)
    settings: SettingsSpec
    policy: PolicySpec
    trusted_organizations: list[TrustedOrgSpec] = []
    knowledge: list[KnowledgeSpec] = []


# --- threads.yaml ---------------------------------------------------------------------


class RequestSpec(_Strict):
    """The network request a seeded thread belongs to (requester side)."""

    brief: Text
    # The request's name, and its one thread's title, which both sides see.
    title: Text | None = None
    created_by: Slug | None = None
    # When the request (and its thread) was made, at or before the anchor: seeded
    # history sits in the world's past (`at: -30d`). Default: when the world is created.
    at: RelativeDate | None = None


class MessageSpec(_Strict):
    sender: Slug = Field(alias="from")
    kind: ThreadMessageKind = "message"
    provenance: Provenance = "agent"
    # The member who wrote it (`human`) or approved it (`agent_approved`), from the
    # sending org.
    member: Slug | None = None
    body: Text
    # When it was sent, at or before the anchor. Default: the previous message's
    # time, or the request's.
    at: RelativeDate | None = None


class AccessSpec(_Strict):
    member: Slug
    reason: AccessReason
    shared_by: Slug | None = None
    question: Text | None = None


class ThreadSpec(_Strict):
    slug: Slug
    requester: Slug
    responder: Slug
    state: ThreadState
    side_to_move: Side | None = None
    request: RequestSpec
    messages: list[MessageSpec] = []
    access: list[AccessSpec] = []


class RecipientSpec(_Strict):
    org: Slug
    reason: Text
    fit: Fit | None = None
    # Its thread's title, which both sides see, and what it alone is asked.
    title: Text | None = None
    ask: Text | None = None
    suggested: bool | None = None


class OpenRequestSpec(_Strict):
    """A request that is open when the world is created, as if its outreach had just
    been approved: seeded with its brief and recipients but no threads, for the
    request's workflow to open and play (`NetworkActivities.start_network_request`).
    It has no chat; its seed ID is `seed_id(scenario, "network_request", slug)`."""

    slug: Slug
    requester: Slug
    created_by: Slug
    # The approved brief: the requester representative's mandate, in prose.
    brief: Text
    # The request's name for the requester's member; never shown to a recipient.
    title: Text | None = None
    recipients: list[RecipientSpec] = Field(min_length=1)


class ThreadsSpec(_Strict):
    threads: list[ThreadSpec] = []
    requests: list[OpenRequestSpec] = []


# --- story.yaml -----------------------------------------------------------------------

# `agents.models.EscalationAction`, restated: importing it would be circular (agents ->
# auth -> worlds -> seeds). tests/unit/test_story.py keeps the two equal.
StoryAction = Literal["approve", "decline", "take_over"]
# A `demo.notice` / `demo.needs_human` kind, such as `needs_human`.
NoticeKind = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.]*$", max_length=64)]


class OutreachApproved(_Strict):
    """The member approved the outreach card."""

    beat: Literal["outreach_approved"]


class InlineQuestion(_Strict):
    """A counterparty's question reached `member` inline in the chat."""

    beat: Literal["inline_question"]
    # The counterparty that asked.
    org: Slug
    member: Slug


class Routed(_Strict):
    """An escalation of `org` was routed to `member`, with a reason."""

    beat: Literal["routed"]
    org: Slug
    member: Slug


class Resolved(_Strict):
    """`member` resolved an escalation of `org` with `action`."""

    beat: Literal["resolved"]
    org: Slug
    member: Slug
    action: StoryAction


class Closed(_Strict):
    """The threads with each of `orgs` are closed."""

    beat: Literal["closed"]
    orgs: list[Slug] = Field(min_length=1)


# What shows that a story step happened; `harmony run` checks each one.
Beat = Annotated[
    OutreachApproved | InlineQuestion | Routed | Resolved | Closed,
    Field(discriminator="beat"),
]


class StoryStepSpec(_Strict):
    """One walkthrough step, as `GET /api/demo/story` shows it."""

    id: Slug
    title: Text
    body: Text
    # Who to act as for this step, when it matters.
    act_as: Slug | None = None
    # The demo event kind that completes the step in the UI.
    done_on: NoticeKind | None = None
    expect: Beat | None = None


class HumanRule(_Strict):
    """What `harmony run` does with an escalation of `org` (the side that decides).
    `member` is who the story expects to decide; whichever of `org`'s members the
    escalation reaches applies the rule. Without `text`, the draft goes out as written."""

    org: Slug
    kind: EscalationKind = "consult"
    # Only escalations on a thread with this org.
    counterparty: Slug | None = None
    member: Slug = Field(alias="as")
    action: StoryAction = "approve"
    text: Text | None = None


class StorySpec(_Strict):
    """`story.yaml`: the suggested story (§11.5). `{+11d}` in its text is a day on the
    world calendar counted from the scenario's anchor (`dates.render_dates`)."""

    name: Slug
    # Who sends the suggested message.
    member: Slug = Field(alias="as")
    suggested_message: Text
    # Ringed and tagged "Suggested" in the network view.
    suggested_recipients: list[Slug] = []
    # What the network can do, in a sentence or two: "What's on screen" shows it over
    # every org.
    network: Text | None = None
    steps: list[StoryStepSpec] = []
    humans: list[HumanRule] = []


# --- Problems -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Problem:
    """One thing wrong with a scenario: which file, where in it, and what."""

    file: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}: {self.path}: {self.message}"


class ScenarioError(Exception):
    """The scenario failed to parse or its references don't resolve."""

    def __init__(self, problems: Sequence[Problem]) -> None:
        self.problems = tuple(problems)
        lines = "\n".join(f"  {problem}" for problem in self.problems)
        super().__init__(f"invalid scenario:\n{lines}")


def yaml_path(loc: Sequence[str | int]) -> str:
    """A validation location as a YAML path: `members[2].role`."""
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else part
    return path or "(top level)"


def problems_from(file: str, error: ValidationError) -> list[Problem]:
    problems: list[Problem] = []
    for detail in error.errors(include_url=False):
        # Our own validators raise ValueError; pydantic prefixes its message.
        message = detail["msg"].removeprefix("Value error, ")
        value: object = detail.get("input")
        if detail["type"] not in {"missing", "extra_forbidden"} and isinstance(
            value, str | int | float | bool | date
        ):
            message += f" (got {value!r})"
        problems.append(Problem(file, yaml_path(detail["loc"]), message))
    return problems


# --- Cross-file references ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourcedOrg:
    file: str
    spec: OrgSpec


def check_references(
    scenario_file: str,
    scenario: ScenarioSpec,
    orgs: Sequence[SourcedOrg],
    threads_file: str | None = None,
    threads: Sequence[ThreadSpec] = (),
    story_file: str | None = None,
    story: StorySpec | None = None,
    requests: Sequence[OpenRequestSpec] = (),
) -> list[Problem]:
    """Every slug reference across the scenario's files, checked; [] when all resolve.
    `requests` are threads.yaml's open requests."""
    problems: list[Problem] = []
    org_members = {org.spec.slug: {m.slug for m in org.spec.members} for org in orgs}

    def report(file: str, path: str, message: str) -> None:
        problems.append(Problem(file, path, message))

    for i, slug in enumerate(scenario.orgs):
        if scenario.orgs.index(slug) != i:
            report(scenario_file, f"orgs[{i}]", f"org {slug!r} is listed twice")
    communities = Counter(c.slug for c in scenario.communities)
    for i, community in enumerate(scenario.communities):
        if communities[community.slug] > 1:
            report(scenario_file, f"communities[{i}].slug", f"duplicate {community.slug!r}")

    owner: dict[str, str] = {}
    for org in orgs:
        _check_org(org, scenario, set(org_members), set(communities), owner, report)

    if scenario.default_member not in owner:
        report(scenario_file, "default_member", f"no member {scenario.default_member!r}")
    if story_file is not None and story is not None:
        _check_story(story_file, story, owner, set(org_members), report)

    if threads_file is not None:
        seen: set[str] = set()
        for i, thread in enumerate(threads):
            if thread.slug in seen:
                report(threads_file, f"threads[{i}].slug", f"duplicate {thread.slug!r}")
            seen.add(thread.slug)
            _check_thread(threads_file, f"threads[{i}]", thread, org_members, report)
            _check_thread_times(threads_file, f"threads[{i}]", thread, scenario.anchor, report)
        for i, request in enumerate(requests):
            # A seeded thread's request shares the seed ID namespace.
            if request.slug in seen:
                report(threads_file, f"requests[{i}].slug", f"duplicate {request.slug!r}")
            seen.add(request.slug)
            _check_request(threads_file, f"requests[{i}]", request, org_members, report)
    return problems


# report(file, yaml_path, message)
type _Report = Callable[[str, str, str], None]


def _check_org(
    org: SourcedOrg,
    scenario: ScenarioSpec,
    org_slugs: set[str],
    communities: set[str],
    owner: dict[str, str],
    report: _Report,
) -> None:
    file, spec = org.file, org.spec
    members: set[str] = set()
    for i, member in enumerate(spec.members):
        if member.slug in owner:
            where = "this org" if owner[member.slug] == spec.slug else f"org {owner[member.slug]!r}"
            report(file, f"members[{i}].slug", f"member {member.slug!r} already exists in {where}")
        owner.setdefault(member.slug, spec.slug)
        members.add(member.slug)

    knowledge: set[str] = set()
    for i, item in enumerate(spec.knowledge):
        if item.slug in knowledge:
            report(file, f"knowledge[{i}].slug", f"duplicate {item.slug!r}")
        knowledge.add(item.slug)
        if item.author is not None and item.author not in members:
            report(file, f"knowledge[{i}].author", f"no member {item.author!r} in this org")

    if spec.policy.default_assignee not in members:
        report(file, "policy.default_assignee", f"no member {spec.policy.default_assignee!r}")
    trusted: set[str] = set()
    for i, row in enumerate(spec.trusted_organizations):
        path = f"trusted_organizations[{i}].org"
        if row.org not in org_slugs or row.org == spec.slug:
            report(file, path, f"no other org {row.org!r}")
        elif row.org in trusted:
            report(file, path, f"duplicate {row.org!r}")
        trusted.add(row.org)
    for i, slug in enumerate(spec.communities):
        if slug not in communities:
            report(file, f"communities[{i}]", f"no community {slug!r} in {scenario.name}")


def _check_thread_times(
    file: str, at: str, thread: ThreadSpec, anchor: date, report: _Report
) -> None:
    """Seeded activity is history: at or before the anchor, and in message order."""
    latest: date | None = None
    stamps = [(f"{at}.request.at", thread.request.at)] + [
        (f"{at}.messages[{i}].at", message.at) for i, message in enumerate(thread.messages)
    ]
    for path, value in stamps:
        if value is None:
            continue
        when = resolve_date(value, anchor)
        if when > anchor:
            report(file, path, "seeded activity cannot be after the scenario's anchor")
        if latest is not None and when < latest:
            report(file, path, "earlier than the request or a message before it")
        latest = max(latest or when, when)


def _check_thread(
    file: str,
    at: str,
    thread: ThreadSpec,
    org_members: dict[str, set[str]],
    report: _Report,
) -> None:
    parties = (thread.requester, thread.responder)
    for field, slug in (("requester", thread.requester), ("responder", thread.responder)):
        if slug not in org_members:
            report(file, f"{at}.{field}", f"no org {slug!r}")
    if thread.requester == thread.responder:
        report(file, f"{at}.responder", "a thread is between two different orgs")

    def member_of(org: str, member: str | None, path: str) -> None:
        if member is not None and org in org_members and member not in org_members[org]:
            report(file, path, f"no member {member!r} in org {org!r}")

    member_of(thread.requester, thread.request.created_by, f"{at}.request.created_by")
    for i, message in enumerate(thread.messages):
        path = f"{at}.messages[{i}]"
        if message.sender not in parties:
            report(file, f"{path}.from", f"{message.sender!r} is not a party to this thread")
        needs_member = message.provenance in {"human", "agent_approved"}
        if needs_member != (message.member is not None):
            report(
                file,
                f"{path}.member",
                "required for provenance human or agent_approved, and only for those",
            )
        member_of(message.sender, message.member, f"{path}.member")

    seen: set[tuple[str, str]] = set()
    for i, access in enumerate(thread.access):
        path = f"{at}.access[{i}]"
        side = next((org for org in parties if access.member in org_members.get(org, ())), None)
        if side is None:
            report(file, f"{path}.member", f"{access.member!r} is not in either party org")
        elif access.shared_by is not None:
            member_of(side, access.shared_by, f"{path}.shared_by")
        if (access.member, access.reason) in seen:
            report(file, path, f"{access.member!r} already has reason {access.reason!r}")
        seen.add((access.member, access.reason))


def _check_request(
    file: str,
    at: str,
    request: OpenRequestSpec,
    org_members: dict[str, set[str]],
    report: _Report,
) -> None:
    members = org_members.get(request.requester)
    if members is None:
        report(file, f"{at}.requester", f"no org {request.requester!r}")
    elif request.created_by not in members:
        report(
            file,
            f"{at}.created_by",
            f"no member {request.created_by!r} in org {request.requester!r}",
        )
    seen: set[str] = set()
    for i, recipient in enumerate(request.recipients):
        path = f"{at}.recipients[{i}].org"
        if recipient.org not in org_members:
            report(file, path, f"no org {recipient.org!r}")
        elif recipient.org == request.requester:
            report(file, path, "a request goes to other orgs")
        elif recipient.org in seen:
            report(file, path, f"duplicate {recipient.org!r}")
        seen.add(recipient.org)


def _check_story(
    file: str, story: StorySpec, owner: dict[str, str], orgs: set[str], report: _Report
) -> None:
    """Every slug in the story names something in the scenario, and every member acts
    for their own org."""

    def org(slug: str, path: str) -> None:
        if slug not in orgs:
            report(file, path, f"no org {slug!r}")

    def member(slug: str, path: str, *, of: str | None = None) -> None:
        if slug not in owner:
            report(file, path, f"no member {slug!r}")
        elif of is not None and of in orgs and owner[slug] != of:
            report(file, path, f"{slug!r} is not a member of {of!r}")

    def dates(text: str, path: str) -> None:
        # Shown text is templated from the anchor; any other `{...}` can't render.
        for template in date_templates(text):
            if not is_relative_date(template):
                report(file, path, f"{{{template}}} is not a relative date such as {{+11d}}")

    member(story.member, "as")
    dates(story.suggested_message, "suggested_message")
    for i, slug in enumerate(story.suggested_recipients):
        org(slug, f"suggested_recipients[{i}]")

    seen: set[str] = set()
    for i, step in enumerate(story.steps):
        at = f"steps[{i}]"
        if step.id in seen:
            report(file, f"{at}.id", f"duplicate {step.id!r}")
        seen.add(step.id)
        dates(step.title, f"{at}.title")
        dates(step.body, f"{at}.body")
        if step.act_as is not None:
            member(step.act_as, f"{at}.act_as")
        expect = step.expect
        if isinstance(expect, InlineQuestion):
            org(expect.org, f"{at}.expect.org")
            member(expect.member, f"{at}.expect.member")
        elif isinstance(expect, Routed | Resolved):
            org(expect.org, f"{at}.expect.org")
            member(expect.member, f"{at}.expect.member", of=expect.org)
        elif isinstance(expect, Closed):
            for j, slug in enumerate(expect.orgs):
                org(slug, f"{at}.expect.orgs[{j}]")

    for i, rule in enumerate(story.humans):
        at = f"humans[{i}]"
        org(rule.org, f"{at}.org")
        member(rule.member, f"{at}.as", of=rule.org)
        if rule.counterparty is not None:
            org(rule.counterparty, f"{at}.counterparty")
            if rule.counterparty == rule.org:
                report(file, f"{at}.counterparty", "an escalation's counterparty is another org")
