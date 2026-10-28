/**
 * Formatting for the Requests inbox: times and the labels the rows show. A date is
 * read off a `Calendar` (the session's world calendar, `useCalendar`), so it matches the
 * World date; a time of day or an age is the same on either calendar.
 */
import type { EndReason } from "../api/types";
import type { BudgetState } from "../app/budget";
import { stoppedLine, type FailureReason } from "../chat/failureCopy";
import type { Calendar } from "../demo/worldClock";
import { firstName, shortOrgName } from "../shell/names";
import type { EscalationKind, InboxItem, OrgRef, Side, ThreadState } from "./types";

// --- Time --------------------------------------------------------------------------------

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** Linear's compact age: "now", "4m", "3h", "2d", then a date. */
export function shortAge(iso: string, calendar: Calendar, now: number = Date.now()): string {
  const elapsed = Math.max(0, now - Date.parse(iso));
  if (elapsed < MINUTE) return "now";
  if (elapsed < HOUR) return `${String(Math.floor(elapsed / MINUTE))}m`;
  if (elapsed < DAY) return `${String(Math.floor(elapsed / HOUR))}h`;
  if (elapsed < 7 * DAY) return `${String(Math.floor(elapsed / DAY))}d`;
  return shortDate(iso, calendar);
}

/** "just now", "4 min ago", "3 hours ago", "2 days ago", then a date. */
export function longAge(iso: string, calendar: Calendar, now: number = Date.now()): string {
  const elapsed = Math.max(0, now - Date.parse(iso));
  if (elapsed < MINUTE) return "just now";
  if (elapsed < HOUR) return `${String(Math.floor(elapsed / MINUTE))} min ago`;
  if (elapsed < DAY) {
    const hours = Math.floor(elapsed / HOUR);
    return `${String(hours)} ${hours === 1 ? "hour" : "hours"} ago`;
  }
  if (elapsed < 7 * DAY) {
    const days = Math.floor(elapsed / DAY);
    return `${String(days)} ${days === 1 ? "day" : "days"} ago`;
  }
  return shortDate(iso, calendar);
}

const dateFormat = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" });
const timeFormat = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit" });

/** "Nov 4". */
export function shortDate(iso: string, calendar: Calendar): string {
  return dateFormat.format(calendar(iso));
}

/** "Nov 4, 9:12 AM". */
export function dateTime(iso: string, calendar: Calendar): string {
  const date = calendar(iso);
  return `${dateFormat.format(date)}, ${timeFormat.format(date)}`;
}

/** "in 19 hours", "in 40 min": how long until `iso`, with no calendar to disagree about. */
export function timeLeft(iso: string, now: number = Date.now()): string {
  const left = Date.parse(iso) - now;
  if (left < MINUTE) return "any moment";
  if (left < HOUR) return `in ${String(Math.floor(left / MINUTE))} min`;
  if (left < DAY) {
    const hours = Math.floor(left / HOUR);
    return `in ${String(hours)} ${hours === 1 ? "hour" : "hours"}`;
  }
  const days = Math.floor(left / DAY);
  return `in ${String(days)} ${days === 1 ? "day" : "days"}`;
}

/** "9:12 AM". */
export function clockTime(iso: string): string {
  return timeFormat.format(new Date(iso));
}

// --- Labels ------------------------------------------------------------------------------

/** The org's representative, as the transcript names it. */
export function representativeName(orgName: string): string {
  return `Claude for ${orgName}`;
}

/** "only Northlake sees this", for a transcript row nobody outside our org sees. */
export function onlyUs(us: OrgRef): string {
  return `only ${shortOrgName(us.name)} sees this`;
}

/** "Henderson Frame Lab's", "Cascade Thin Films'". */
export function possessive(name: string): string {
  return name.endsWith("s") ? `${name}'` : `${name}'s`;
}

/** What an open escalation of `kind` asks of the member: the inbox row's subtitle. */
export function needsLabel(kind: EscalationKind): string {
  switch (kind) {
    case "consult":
      return "Needs your answer";
    case "outbound_review":
      return "Reply needs your review";
  }
}

export function isTerminal(state: ThreadState): boolean {
  return state === "closed" || state === "expired";
}

/** A thread's position: its state, our side, and whose turn it is. */
export interface Standing {
  state: ThreadState;
  side: Side;
  side_to_move: Side | null;
}

/**
 * Where a thread stands, from our side. While the move is the counterparty's it reads
 * "Waiting on {them}", whether their Claude is writing or checking with its own team;
 * however it ended, it reads "Closed" (REOPEN.md §2.2).
 */
export function stateLabel({ state, side, side_to_move }: Standing, counterparty: string): string {
  if (isTerminal(state)) return "Closed";
  if (side_to_move !== null && side_to_move !== side) return `Waiting on ${counterparty}`;
  return state === "awaiting_principal" ? "Checking with the team" : "Claude is handling";
}

/**
 * The inbox row's subtitle after the counterparty's name. `needsYou` is the row's
 * section: an open item assigned to someone else reads as theirs, not yours. As in the
 * view's pill, a thread that ended says so before who shared it.
 */
export function rowStatus(item: InboxItem, needsYou: boolean): string {
  const open = item.escalation !== null && item.escalation.status === "open" ? item.escalation : null;
  if (open !== null && needsYou) return needsLabel(open.kind);
  if (item.stop_reason != null || isTerminal(item.thread_state)) return "Closed";
  if (item.shared_by !== null) return `Shared by ${firstName(item.shared_by.name)}`;
  if (open !== null && open.assignees.length > 0) {
    return `Waiting on ${open.assignees.map((assignee) => firstName(assignee.name)).join(", ")}`;
  }
  return stateLabel({ state: item.thread_state, side: item.side, side_to_move: item.side_to_move }, item.counterparty.name);
}

// --- Why a run ended ---------------------------------------------------------------------

/** How one run of a thread ended: `ThreadRunView.ended`, or the thread's own fields. */
export interface Ending {
  endReason: EndReason | null;
  closedBy: Side | null;
  stopReason: FailureReason | null;
}

const REASON: Record<Exclude<EndReason, "closed" | "stopped">, string> = {
  quiet: "both sides went quiet",
  unsent: "nothing was sent",
  turn_limit: "it reached its turn limit",
  deadline: "it reached its deadline",
};

/**
 * Why a run ended, in the words after "Closed · {date} ·" (REOPEN.md §2.1). `closer`
 * names the side that closed it; a close nobody is named for (a seeded one) and a stop
 * with no reason say nothing more.
 */
export function reasonLine(
  { endReason, stopReason }: Pick<Ending, "endReason" | "stopReason">,
  closer: string | null,
  budget: BudgetState,
): string | null {
  switch (endReason) {
    case null:
      return null;
    case "closed":
      return closer === null ? null : `${closer} closed it`;
    case "stopped":
      return stopReason === null ? null : stoppedLine(stopReason, budget);
    default:
      return REASON[endReason];
  }
}

/** `reasonLine` from our side: our own close reads "Claude closed it", theirs names them. */
export function endLine(ending: Ending, side: Side, counterparty: string, budget: BudgetState): string | null {
  const closer = ending.closedBy === null ? null : ending.closedBy === side ? "Claude" : counterparty;
  return reasonLine(ending, closer, budget);
}
