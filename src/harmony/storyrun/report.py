"""What `harmony run` prints when the story stops: which beats were seen, the chat and
each thread as their participants saw them, every agent turn with its latency and
cost, then how the engine ran (the models used, latency, failures and retries, the
run's spend with its peaks, the worker's memory, and `storyrun.checks`) and harmony's
spend (`harmony spend`, with its new alerts).

Collection reads through the API as the story's member, plus the world's rows (no route
serves those). Formatting is pure.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.agents.models import (
    AssistantTextItem,
    ChatItem,
    InlineQuestionItem,
    OutreachCardItem,
    RequestStatusItem,
    ThinkingSummaryItem,
    ToolActivityItem,
    UserTextItem,
    brief_title,
)
from harmony.api.routes.conversations import ConversationDetail
from harmony.api.routes.threads import ThreadDetail
from harmony.config import Settings
from harmony.db.models import AgentTurn, ModelCall, Org, Thread
from harmony.llm.errors import LedgerUnavailable
from harmony.llm.ledger import SpendLedger
from harmony.storyrun.beats import StoryRun, describe
from harmony.storyrun.spend import (
    RunSpend,
    as_measured,
    at_production_prices,
    peak_usd,
    percentile_ms,
    spent_by,
)
from harmony.temporal.workflows.payloads import BUDGET_EXHAUSTED_ERROR

if TYPE_CHECKING:
    from harmony.storyrun.worker import ActivityLog

RULE = "-" * 72


@dataclass(frozen=True, slots=True)
class TurnRow:
    at: datetime
    role: str
    org: str
    side: str | None
    latency_ms: int | None
    cost_usd: Decimal


@dataclass(frozen=True, slots=True)
class CallTotals:
    calls: int
    cost_usd: Decimal


# --- Collecting -----------------------------------------------------------------------


async def agent_turns(
    sessionmaker: async_sessionmaker[AsyncSession], world_id: UUID
) -> tuple[list[TurnRow], CallTotals]:
    async with sessionmaker() as session:
        rows = await session.execute(
            select(
                AgentTurn.created_at,
                AgentTurn.role,
                Org.slug,
                AgentTurn.side,
                AgentTurn.latency_ms,
                AgentTurn.cost_usd,
            )
            .join(Org, (Org.world_id == AgentTurn.world_id) & (Org.id == AgentTurn.org_id))
            .where(AgentTurn.world_id == world_id)
            .order_by(AgentTurn.created_at, AgentTurn.id)
        )
        turns = [TurnRow(*row) for row in rows]
        calls, cost = (
            await session.execute(
                select(func.count(), func.coalesce(func.sum(ModelCall.cost_usd), 0)).where(
                    ModelCall.world_id == world_id
                )
            )
        ).one()
    return turns, CallTotals(int(calls), Decimal(cost))


async def stop_reasons(
    sessionmaker: async_sessionmaker[AsyncSession], world_id: UUID
) -> Counter[str]:
    """Why the world's threads stopped short, by `stop_reason`."""
    async with sessionmaker() as session:
        reasons = await session.scalars(
            select(Thread.stop_reason).where(
                Thread.world_id == world_id, Thread.stop_reason.is_not(None)
            )
        )
        return Counter(reason for reason in reasons if reason is not None)


def ledger_total(settings: Settings) -> Decimal | None:
    """The spend ledger's total so far, or None without a readable ledger."""
    path = settings.spend_ledger_path
    if path is None:
        return None
    try:
        return (
            SpendLedger(path.expanduser().absolute(), project=settings.spend_project)
            .read()
            .spent_usd
        )
    except LedgerUnavailable:
        return None


# --- Formatting -----------------------------------------------------------------------


def section(title: str) -> list[str]:
    return ["", RULE, title, RULE]


def beat_lines(run: StoryRun) -> list[str]:
    """Each expected step, seen or missing, then any beat the steps didn't ask for."""
    when = {beat.atom: beat.elapsed_s for beat in run.seen}
    lines: list[str] = []
    for step, wanted in run.expected:
        times = [when[atom] for atom in wanted if atom in when]
        if len(times) == len(wanted):
            lines.append(f"  ok       {max(times):6.1f}s  {step.title}")
        else:
            lines.append(f"  MISSING          {step.title}")
            lines += [
                f"           waiting for: {describe(a)}" for a in sorted(wanted - when.keys())
            ]
    expected = {atom for _, wanted in run.expected for atom in wanted}
    lines += [
        f"  also     {beat.elapsed_s:6.1f}s  {describe(beat.atom)}"
        for beat in run.seen
        if beat.atom not in expected
    ]
    return lines


def chat_lines(chat: ConversationDetail) -> list[str]:
    lines = [f"{chat.title} ({chat.owner.name})"]
    for item in chat.items:
        lines += chat_item_lines(item, chat.owner.name)
    return lines


def chat_item_lines(item: ChatItem, owner: str) -> list[str]:
    match item:
        case UserTextItem(text=text):
            return ["", f"{owner}:", *_indent(text)]
        case AssistantTextItem(markdown=markdown):
            return ["", "Claude:", *_indent(markdown)]
        case ThinkingSummaryItem(text=text):
            return [f"  (thinking) {text}"]
        case ToolActivityItem(label=label, detail=detail, status=status, outcome=outcome):
            line = f"  [{status}] {label}" + (f" · {outcome}" if outcome else "")
            return [line, *(_indent(detail, "      ") if detail else [])]
        case OutreachCardItem():
            names = ", ".join(r.org.name for r in item.recipients if r.selected)
            title = brief_title(item.brief)
            lines = [f"  [outreach card, {item.status}] {title}", f"    to: {names}"]
            return lines + ([f"    feedback: {item.feedback}"] if item.feedback else [])
        case RequestStatusItem(request_id=request_id):
            return [f"  [request status] {request_id}"]
        case InlineQuestionItem():
            settled = f"{item.status}: {item.action}" if item.action else item.status
            lines = [f"  [question from {item.from_org.name}, {settled}] {item.question}"]
            return lines + ([f"    answer: {item.answer}"] if item.answer else [])


def thread_lines(thread: ThreadDetail) -> list[str]:
    names = {thread.requester.id: thread.requester.name, thread.responder.id: thread.responder.name}
    lines = [f"{thread.title} ({thread.state})"]
    for message in thread.messages:
        who = names.get(message.from_org_id, str(message.from_org_id))
        label = (
            message.kind
            if message.provenance == "agent"
            else f"{message.kind}, {message.provenance}"
        )
        if message.kind == "close" and not message.body:
            # The marker a `close` leaves; the summary stays with the closing side.
            lines += ["", f"{message.seq}. {who} closed the conversation ({label})."]
        else:
            lines += ["", f"{message.seq}. {who} ({label}):", *_indent(message.body)]
    return lines


def turn_lines(turns: Iterable[TurnRow], totals: CallTotals) -> list[str]:
    turns = list(turns)
    calls = f"{totals.calls} model calls, ${totals.cost_usd:.4f}"
    if not turns:
        return [f"no agent turns recorded; {calls}"]
    start = turns[0].at
    lines = [f"{'at':>7}  {'role':<14} {'org':<12} {'side':<9} {'latency':>9} {'cost':>9}"]
    for turn in turns:
        latency = f"{turn.latency_ms / 1000:.1f}s" if turn.latency_ms is not None else "-"
        lines.append(
            f"{(turn.at - start).total_seconds():6.1f}s  {turn.role:<14} {turn.org:<12} "
            f"{turn.side or '-':<9} {latency:>9} ${turn.cost_usd:>8.4f}"
        )
    total = sum((t.cost_usd for t in turns), Decimal(0))
    lines.append(f"{len(turns)} turns, ${total:.4f}; {calls}")
    return lines


def model_lines(models: Sequence[str], override: str | None) -> tuple[list[str], bool]:
    """The models the calls asked for, and whether the setup failed: with an override
    set, any other model means the run didn't play on the model it was meant to."""
    stray = [model for model in models if override is not None and model != override]
    lines = [f"  models used: {', '.join(models) or 'none'}"]
    if stray:
        lines.append(f"  SETUP FAILURE: {', '.join(stray)} isn't the override {override}")
    return lines, bool(stray)


def latency_lines(latencies_ms: Sequence[int]) -> list[str]:
    def seconds(percent: float) -> str:
        value = percentile_ms(latencies_ms, percent)
        return "-" if value is None else f"{value / 1000:.1f}s"

    return [f"  turn latency: p50 {seconds(50)}, p95 {seconds(95)} (of {len(latencies_ms)} turns)"]


def failure_lines(refusals: int, stopped: Mapping[str, int], log: ActivityLog | None) -> list[str]:
    """Refusals and threads that stopped short from the world's rows; failed attempts
    and retries from the in-process worker's log."""
    lines = [
        f"  refusals: {refusals}",
        "  threads stopped short: "
        + (", ".join(f"{reason} {n}" for reason, n in sorted(stopped.items())) or "none"),
    ]
    if log is None:
        return [*lines, "  failed attempts and retries: n/a (external worker)"]
    budget = {
        label: n for label, n in log.errors.items() if label.startswith(BUDGET_EXHAUSTED_ERROR)
    }
    return [
        *lines,
        f"  TurnIncomplete: {log.errors['TurnIncomplete']}",
        "  BudgetExhausted: "
        + (", ".join(f"{label} {n}" for label, n in sorted(budget.items())) or "0"),
        f"  model retries: {log.retries}",
        "  failed attempts: "
        + (", ".join(f"{label} {n}" for label, n in sorted(log.errors.items())) or "none"),
    ]


def run_spend_lines(
    spend: RunSpend,
    run: StoryRun,
    *,
    ledger_before: Decimal | None,
    ledger_after: Decimal | None,
    measured_peak_usd: Decimal | None,
) -> list[str]:
    """The run's spend: the ledger's, the world's at each beat, and the lineage's peak
    `spent + reserved`, measured and re-priced at production prices (3.14)."""
    ledger = (
        f"${ledger_after - ledger_before:.4f}"
        if ledger_before is not None and ledger_after is not None
        else "n/a (no spend ledger)"
    )
    measured = (
        f"${measured_peak_usd:.4f} (sampled from the hosted budget row)"
        if measured_peak_usd is not None
        else f"${peak_usd(spend.turns, as_measured):.4f} (approximated from the model calls)"
    )
    return [
        f"  ledger spend this run: {ledger}",
        f"  world spend: ${sum((call.cost_usd for call in spend.calls), Decimal(0)):.4f}",
        "  at each beat:",
        *(
            f"    {beat.elapsed_s:6.1f}s  ${spent_by(spend.calls, beat.at):.4f}  "
            f"{describe(beat.atom)}"
            for beat in run.seen
        ),
        f"  lineage peak spent + reserved: {measured}",
        f"  re-priced at production prices: ${peak_usd(spend.turns, at_production_prices):.4f}",
    ]


def rss_lines(rss_mb: float | None) -> list[str]:
    if rss_mb is None:
        return ["  peak RSS: n/a (external worker)"]
    return [f"  peak RSS (in-process worker, app and player): {rss_mb:.0f} MB"]


def spend_lines(settings: Settings) -> list[str]:
    """`harmony spend` and its new alerts, read-only."""
    from harmony.cli_spend import alert_line, report_lines

    path = settings.spend_ledger_path
    if path is None:
        return ["SPEND_LEDGER_PATH is not set"]
    ledger = SpendLedger(path.expanduser().absolute(), project=settings.spend_project)
    try:
        summary = ledger.read()
    except LedgerUnavailable as exc:
        return [f"spend ledger unavailable: {exc}"]
    lines = report_lines(summary, settings.dev_budget_usd)
    return lines + ([alert_line(a) for a in summary.unacked] or ["no new alerts"])


def _indent(text: str, pad: str = "    ") -> list[str]:
    return [pad + line if line else "" for line in text.splitlines()] or [pad]
