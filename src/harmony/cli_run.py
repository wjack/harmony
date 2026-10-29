"""`harmony run <scenario>`: play a scenario's story headless in a fresh world.

Seeds a world through the API (`POST /api/session/start`), sends the story's message
as its member (`--as` another member of that org, with `--live`), and acts where people
would: approving outreach cards and resolving escalations by the story's `humans`
rules or as drafted (`--interactive` asks instead, `--approve all` approves every listed
recipient). `--message TEXT` sends that instead, as `--as` or the story's member, and
follows whatever requests come of it to their end (the 10-org sim: `--message ... --as
<member> --approve all`). `--send-without-confirmation` starts the chat sending without
review, and `--skip-approvals` turns Demo controls' Skip approvals on, so nobody is
asked. `--edit ORG:COUNTERPARTY:N:TEXT` has a member of ORG tell its Claude TEXT (an
Edit) once ORG's thread with COUNTERPARTY has N messages, and the report's
`guidance_followed` shows what that side sent next (EDIT.md §9). Prints each beat as it
is seen, waits for the requests to close and Claude's summary, then prints the
transcripts, every agent turn with its latency and cost, how the engine ran
(`storyrun.report`, `storyrun.checks`) and harmony's spend.

The app runs in process. So does the Temporal worker, playing the cassettes
(`MODEL_MODE=scripted`, the default) or calling the API (`--live`, which spends). It
refuses to start while another worker polls the task queues. `--worker external` plays
the story on that worker instead (`make dev`'s), with its cassettes. The world is
retired at the end unless `--keep`.

Exit codes: 0 every expected beat was seen (or, for `--message`, every request
ended); 1 the story stopped short (timeout, a missing beat, a failed model call, an API
error); 2 it could not start, or its calls asked for a model other than the override.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

import typer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from harmony.agents.keys import chat_key
from harmony.config import Settings, get_settings
from harmony.db.engine import create_sessionmaker, engine_from_settings
from harmony.seeds.dates import render_dates
from harmony.seeds.loader import (
    DEFAULT_SCENARIO,
    Scenario,
    ScenarioNotFound,
    available_scenarios,
    load_scenario,
)
from harmony.seeds.schema import ScenarioError, StorySpec
from harmony.storyrun.beats import free_form, recast
from harmony.storyrun.edits import PlannedEdit, parse_edit

if TYPE_CHECKING:
    from decimal import Decimal

    from harmony.storyrun.beats import StoryRun
    from harmony.storyrun.decide import Decisions
    from harmony.storyrun.play import StoryPlayer
    from harmony.storyrun.worker import ActivityLog, InProcessWorker
    from harmony.worlds.retirer import Retirer

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)

WorkerChoice = Literal["in-process", "external"]
ApproveChoice = Literal["story", "all"]
RETIRE_WAIT_S = 60.0


class SetupError(Exception):
    """The run can't start; exit 2."""


@dataclass(frozen=True, slots=True)
class RunOptions:
    scenario: str
    story: str
    live: bool
    member: str | None
    interactive: bool
    worker: WorkerChoice
    timeout_s: float
    keep: bool
    message: str | None = None
    approve: ApproveChoice = "story"
    send_without_confirmation: bool = False
    skip_approvals: bool = False
    # `--edit` as typed: `planned_edits` reads them once the scenario is loaded.
    edits: tuple[str, ...] = ()


@app.command()
def run(
    scenario: Annotated[str, typer.Argument(help="Scenario directory name.")] = DEFAULT_SCENARIO,
    story: Annotated[
        str, typer.Option(help="The story to play (story.yaml's name).")
    ] = "suggested",
    live: Annotated[
        bool, typer.Option("--live", help="Call the Anthropic API instead of the cassettes.")
    ] = False,
    member: Annotated[
        str | None,
        typer.Option(
            "--as",
            help="Play the story's member as another member of their org (needs --live: "
            "the cassettes play the story's own member). With --message, the member who "
            "sends it.",
        ),
    ] = None,
    message: Annotated[
        str | None,
        typer.Option(
            "--message",
            help="Send this instead of the story, and follow its requests to their end.",
        ),
    ] = None,
    approve: Annotated[
        str,
        typer.Option(
            help="story: approve the recipients the story or the card suggests; all: every "
            "listed recipient, and every escalation as drafted.",
        ),
    ] = "story",
    send_without_confirmation: Annotated[
        bool,
        typer.Option(
            "--send-without-confirmation",
            help="Start the chat sending requests without review.",
        ),
    ] = False,
    skip_approvals: Annotated[
        bool,
        typer.Option(
            "--skip-approvals",
            help="Turn Demo controls' Skip approvals on: nobody in the world is asked.",
        ),
    ] = False,
    edit: Annotated[
        list[str] | None,
        typer.Option(
            "--edit",
            help="ORG:COUNTERPARTY:N:TEXT: once ORG's thread with COUNTERPARTY has N "
            "messages, a member of ORG tells its Claude TEXT (an Edit). Repeatable.",
        ),
    ] = None,
    interactive: Annotated[
        bool,
        typer.Option("--interactive", help="Ask at each decision instead of following the story."),
    ] = False,
    worker: Annotated[
        str,
        typer.Option(
            help="in-process, or external to play on a worker already polling.",
        ),
    ] = "in-process",
    timeout: Annotated[float, typer.Option(help="Seconds to wait for the story.", min=1)] = 600,
    keep: Annotated[bool, typer.Option("--keep", help="Leave the world live afterwards.")] = False,
) -> None:
    """Run a scenario's story headless in a fresh world and report each beat."""
    if worker not in ("in-process", "external"):
        typer.echo("--worker is in-process or external", err=True)
        raise typer.Exit(code=2)
    if approve not in ("story", "all"):
        typer.echo("--approve is story or all", err=True)
        raise typer.Exit(code=2)
    options = RunOptions(
        scenario=scenario,
        story=story,
        live=live,
        member=member,
        interactive=interactive,
        worker="external" if worker == "external" else "in-process",
        timeout_s=timeout,
        keep=keep,
        message=message,
        approve="all" if approve == "all" else "story",
        send_without_confirmation=send_without_confirmation,
        skip_approvals=skip_approvals,
        edits=tuple(edit or ()),
    )
    try:
        if interactive and options.approve == "all":
            raise SetupError("--interactive asks at each decision; drop --approve all")
        loaded, spec = load_story(options)
        edits = planned_edits(loaded, options.edits)
        settings = run_settings(get_settings(), options)
        code = asyncio.run(_run(options, loaded, spec, settings, edits))
    except SetupError as exc:
        typer.echo(f"harmony run: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    raise typer.Exit(code=code)


def load_story(options: RunOptions) -> tuple[Scenario, StorySpec]:
    """The scenario, and the story as this run plays it (`--message`, `--as`)."""
    try:
        scenario = load_scenario(options.scenario)
    except ScenarioNotFound as exc:
        known = ", ".join(available_scenarios()) or "none"
        raise SetupError(f"{exc} (available: {known})") from exc
    except ScenarioError as exc:
        raise SetupError(str(exc)) from exc
    if options.message is not None:
        return scenario, message_story(scenario, options.message, options.member)
    story = scenario.story
    if story is None:
        raise SetupError(f"scenario {scenario.name} has no story.yaml")
    if story.name != options.story:
        raise SetupError(
            f"scenario {scenario.name} has story {story.name!r}, not {options.story!r}"
        )
    return scenario, cast_story(scenario, story, options)


def planned_edits(scenario: Scenario, values: tuple[str, ...]) -> tuple[PlannedEdit, ...]:
    slugs = frozenset(org.slug for org in scenario.orgs)
    try:
        return tuple(parse_edit(value, slugs) for value in values)
    except ValueError as exc:
        raise SetupError(f"--edit: {exc}") from exc


def message_story(scenario: Scenario, message: str, member: str | None) -> StorySpec:
    """`--message` as a story with no beats, sent by `member`, else by the scenario
    story's member. Any member of any org may send it; on the cassettes, only a chat
    they key (`chat:<member>:1`) plays."""
    sender = member or (scenario.story.member if scenario.story is not None else None)
    if sender is None:
        raise SetupError(f"--message: scenario {scenario.name} has no story; pass --as")
    slugs = [m.slug for org in scenario.orgs for m in org.members]
    if sender not in slugs:
        raise SetupError(f"--as {sender}: no member {sender!r} in {scenario.name}")
    try:
        render_dates(message, scenario.anchor)
    except ValueError as exc:
        raise SetupError(f"--message: {exc}") from exc
    return free_form(sender, message)


def cast_story(scenario: Scenario, story: StorySpec, options: RunOptions) -> StorySpec:
    """The story as `--as` plays it. The stand-in must belong to the story member's
    org (it is that org's request), and scripted runs keep the story's own member: the
    cassettes key the chat by its owner."""
    member = options.member
    if member is None or member == story.member:
        return story
    org = next(o for o in scenario.orgs if any(m.slug == story.member for m in o.members))
    slugs = [m.slug for m in org.members]
    if member not in slugs:
        raise SetupError(
            f"--as {member}: the story is {org.name}'s request; play it as one of "
            + ", ".join(slugs)
        )
    if not options.live:
        raise SetupError(
            f"--as {member}: the cassettes play {story.member}'s chat "
            f"({chat_key(story.member, 1)}); add --live to play it as {member}"
        )
    return recast(story, member)


def run_settings(settings: Settings, options: RunOptions) -> Settings:
    if options.live:
        if options.worker == "external":
            raise SetupError("--live plays on the in-process worker; drop --worker external")
        return settings.model_copy(update={"model_mode": "live"})
    if settings.model_mode != "scripted":
        raise SetupError(f"MODEL_MODE is {settings.model_mode}; pass --live to run live")
    if not settings.cassette_dir.is_dir():
        raise SetupError(f"no cassettes at {settings.cassette_dir}")
    return settings


async def _run(
    options: RunOptions,
    scenario: Scenario,
    story: StorySpec,
    settings: Settings,
    edits: tuple[PlannedEdit, ...],
) -> int:
    # Imported here: `harmony run --help` shouldn't load the app, Temporal or the SDK.
    from harmony.storyrun import report
    from harmony.storyrun.api import ApiError, in_process
    from harmony.storyrun.play import StoryPlayer
    from harmony.worlds.retirer_temporal import TemporalRetirer

    engine = engine_from_settings(settings, application_name="harmony-run")
    sessionmaker = create_sessionmaker(engine)
    ledger_before = report.ledger_total(settings)
    try:
        async with _temporal(options, settings, sessionmaker) as (client, worker):
            # Its transcripts are printed by then, so the rows needn't stay readable.
            retirer = TemporalRetirer(client, sessionmaker, row_ttl=timedelta(0))
            async with in_process(settings, engine=engine, temporal=client, retirer=retirer) as api:
                typer.echo(
                    f"harmony run {scenario.name}: story {story.name} as {story.member}, "
                    f"model {settings.model_mode}, worker {options.worker}"
                )
                player = StoryPlayer(
                    api,
                    scenario,
                    story,
                    _decisions(options, story),
                    log=typer.echo,
                    timeout_s=options.timeout_s,
                    worker=worker.serving if worker is not None else None,
                    failures=worker.log.failures if worker is not None else None,
                    send_without_review=options.send_without_confirmation,
                    skip_approvals=options.skip_approvals,
                    edits=edits,
                )
                try:
                    result = await player.play()
                    if result.outcome == "done" and not await player.settle(result):
                        typer.echo("the requests hadn't closed with Claude's summary yet")
                    return await _report(
                        player,
                        result,
                        RunMeasures(
                            scenario=scenario.name,
                            log=worker.log if worker is not None else None,
                            ledger_before=ledger_before,
                        ),
                        settings,
                        sessionmaker,
                    )
                except (ApiError, OSError, SQLAlchemyError) as exc:
                    if player.world_id is not None:
                        raise
                    typer.echo(f"harmony run: couldn't start a world: {exc}", err=True)
                    return 2
                finally:
                    # Even after a crash: a world left live keeps its workflows waiting.
                    if player.world_id is not None:
                        await _retire(options, client, retirer, worker, player.world_id)
    finally:
        await engine.dispose()


def _decisions(options: RunOptions, story: StorySpec) -> Decisions:
    from harmony.storyrun.decide import ApproveAll, PromptDecisions, StoryDecisions

    if options.interactive:
        return PromptDecisions(story)
    return ApproveAll() if options.approve == "all" else StoryDecisions(story)


@dataclass(frozen=True, slots=True)
class RunMeasures:
    """What the run measured while it played, for the report."""

    scenario: str
    # The in-process worker's failed attempts; None on an external worker.
    log: ActivityLog | None
    ledger_before: Decimal | None


async def _report(
    player: StoryPlayer,
    result: StoryRun,
    measures: RunMeasures,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> int:
    from harmony.storyrun import report

    chat, threads = await player.transcripts(result)
    _echo(report.section("Beats") + report.beat_lines(result))
    verdict = "complete" if result.outcome == "done" else result.outcome
    typer.echo(f"story {verdict}" + (f": {result.detail}" if result.detail else ""))
    if chat is not None:
        _echo(report.section("Chat") + report.chat_lines(chat))
    for thread in threads:
        title = f"Thread: {thread.requester.name} and {thread.responder.name}"
        _echo(report.section(title) + report.thread_lines(thread))
    _echo(report.section("Agent turns"))
    try:
        turns, totals = await report.agent_turns(sessionmaker, result.world_id)
        _echo(report.turn_lines(turns, totals))
    except SQLAlchemyError as exc:
        typer.echo(f"agent turns unavailable: {exc}")
    setup_failed = await _engine_report(result, measures, settings, sessionmaker)
    _echo(report.section("Spend") + report.spend_lines(settings))
    if setup_failed:
        return 2
    return 0 if result.outcome == "done" else 1


async def _engine_report(
    result: StoryRun,
    measures: RunMeasures,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> bool:
    """Print how the engine ran and its checks. True if the run asked for a model
    other than the override (a setup failure)."""
    from harmony.storyrun import report
    from harmony.storyrun.checks import engine_check_lines, engine_checks
    from harmony.storyrun.spend import peak_rss_mb, run_spend

    _echo(report.section("Engine"))
    try:
        spend = await run_spend(sessionmaker, result.world_id)
        stopped = await report.stop_reasons(sessionmaker, result.world_id)
        checks = await engine_checks(sessionmaker, result.world_id, measures.scenario)
    except SQLAlchemyError as exc:
        typer.echo(f"engine figures unavailable: {exc}")
        return False
    models, setup_failed = report.model_lines(spend.models, settings.model_override)
    _echo(
        models
        + report.latency_lines(spend.latencies_ms)
        + report.failure_lines(spend.refusals, stopped, measures.log)
        + report.run_spend_lines(
            spend,
            result,
            ledger_before=measures.ledger_before,
            ledger_after=report.ledger_total(settings),
        )
        + report.rss_lines(peak_rss_mb() if measures.log is not None else None)
    )
    _echo(report.section("Engine checks") + engine_check_lines(checks))
    return setup_failed


async def _retire(
    options: RunOptions,
    client: Client,
    retirer: Retirer,
    worker: InProcessWorker | None,
    world_id: UUID,
) -> None:
    if options.keep:
        typer.echo(f"\nworld {world_id} kept")
        return
    if worker is not None:
        worker.log.retiring(world_id)
    await retirer.retire(world_id)
    done = worker is None or worker.serving.done() or await _retired(client, world_id)
    state = "retired" if done else "retiring (the next worker finishes it)"
    typer.echo(f"\nworld {world_id} {state}")


@asynccontextmanager
async def _temporal(
    options: RunOptions,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[Client, InProcessWorker | None]]:
    """A Temporal client and, unless external, this run's own worker."""
    from harmony.storyrun.worker import WorkerBusy, ensure_no_pollers, identity, running_worker
    from harmony.temporal.client import connect_client
    from harmony.temporal.worker import WorkerDeps, model_runtime

    try:
        # Before connecting: a live runtime without its spend ledger refuses to build.
        runtime = model_runtime(settings, sessionmaker) if options.worker == "in-process" else None
        client = await connect_client(settings, identity=identity("harmony-run"))
        if runtime is not None:
            await ensure_no_pollers(client)
    except WorkerBusy as exc:
        raise SetupError(str(exc)) from exc
    except (RuntimeError, OSError, ValueError) as exc:
        raise SetupError(f"can't start: {exc}") from exc
    if runtime is None:
        yield client, None
        return
    async with running_worker(WorkerDeps(settings, client, sessionmaker, runtime)) as worker:
        yield client, worker


async def _retired(client: Client, world_id: UUID) -> bool:
    """Wait for this run's own worker to finish retiring the world, within reason."""
    from harmony.temporal.workflows.retire_world import retire_workflow_id

    handle = client.get_workflow_handle(retire_workflow_id(world_id))
    try:
        await asyncio.wait_for(handle.result(), RETIRE_WAIT_S)
    except TimeoutError:
        return False
    return True


def _echo(lines: list[str]) -> None:
    for line in lines:
        typer.echo(line)
