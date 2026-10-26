"""What `harmony run` prints when the story stops: which beats were seen, the chat and
each thread as their participants saw them, every agent turn with its latency and
cost, and harmony's spend (`harmony spend`, with its new alerts).

Collection reads through the API as the story's member, plus `agent_turns` and
`model_calls` for the world (no route serves those). Formatting is pure.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
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
from harmony.db.models import AgentTurn, ModelCall, Org
from harmony.llm.errors import LedgerUnavailable
from harmony.llm.ledger import SpendLedger
from harmony.storyrun.beats import StoryRun, describe

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
        turns = [TurnRow(*row) for row in rows.tuples()]
        calls, cost = (
            await session.execute(
                select(func.count(), func.coalesce(func.sum(ModelCall.cost_usd), 0)).where(
                    ModelCall.world_id == world_id
                )
            )
        ).one()
    return turns, CallTotals(int(calls), Decimal(cost))


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
        case ToolActivityItem(label=label, detail=detail, status=status):
            return [f"  [{status}] {label}" + (f" · {detail}" if detail else "")]
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
