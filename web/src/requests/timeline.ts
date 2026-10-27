/**
 * The thread view's transcript: what crossed the network (both sides see it), how each
 * run ended and who reopened it, merged with what happened on our side of the firewall
 * (releases, resolutions, our edits and comments), in time order. Pure, so the ordering
 * rules are unit-tested.
 */
import type { SteerView } from "../api/types";
import { isTerminal, type Ending } from "./format";
import type {
  ConsultView,
  EscalationView,
  MemberRef,
  NoteView,
  OrgRef,
  Provenance,
  ThreadDetail,
  ThreadMessage,
  ThreadWorkspace,
} from "./types";

export interface MessageEntry {
  type: "message";
  id: string;
  at: string;
  /** Sent by our side's representative (or one of us). */
  ours: boolean;
  org: OrgRef;
  provenance: Provenance;
  body: string;
  /** Who approved or wrote it, for `agent_approved` and `human` messages on our side. */
  approvedBy: MemberRef | null;
}

/** A side's representative closed the thread (its summary stays with its own org). */
export interface CloseEntry {
  type: "close";
  id: string;
  at: string;
  ours: boolean;
  org: OrgRef;
}

/** A consult answer that went back to our representative, for this thread only. */
export interface ReleaseEntry {
  type: "release";
  id: string;
  at: string;
  consult: ConsultView;
  /** Null when Claude released it without a teammate. */
  by: MemberRef | null;
}

export interface ResolutionEntry {
  type: "resolution";
  id: string;
  at: string;
  escalation: EscalationView;
  by: MemberRef | null;
}

/** A comment for our team, which no Claude reads. */
export interface NoteEntry {
  type: "note";
  id: string;
  at: string;
  note: NoteView;
}

/** One of our Edits: an instruction to our Claude (EDIT.md §8.3). Reopen attempts are reopen rows instead. */
export interface SteerEntry {
  type: "steer";
  id: string;
  at: string;
  steer: SteerView;
}

/** A run of the conversation ended (REOPEN.md §8.1). The latest carries Reopen. */
export interface EndedEntry {
  type: "ended";
  id: string;
  at: string;
  run: number;
  ending: Ending;
  /** Our side's summary of this run: the current one for the latest, else the archived one. */
  summary: string | null;
  /** The run that ended last, while the thread is still closed. */
  latest: boolean;
}

/**
 * A side reopened the conversation, or one of ours is trying to. The counterparty's
 * reopen names only its org; our attempts carry the steer that holds what was typed.
 */
export interface ReopenEntry {
  type: "reopen";
  id: string;
  at: string;
  org: OrgRef;
  ours: boolean;
  by: MemberRef | null;
  steer: SteerView | null;
  outcome: "working" | "opened" | "failed";
}

export type TimelineEntry =
  | MessageEntry
  | CloseEntry
  | ReleaseEntry
  | ResolutionEntry
  | NoteEntry
  | SteerEntry
  | EndedEntry
  | ReopenEntry;

/** Our org and the counterparty, from the transcript's side. */
export function partiesOf(thread: Pick<ThreadDetail, "side" | "requester" | "responder">): {
  us: OrgRef;
  them: OrgRef;
} {
  return thread.side === "requester"
    ? { us: thread.requester, them: thread.responder }
    : { us: thread.responder, them: thread.requester };
}

/** The member who resolved the escalation that released `message`, if one did. */
function approvedByOf(message: ThreadMessage, escalations: readonly EscalationView[]): MemberRef | null {
  if (message.provenance !== "agent_approved" && message.provenance !== "human") return null;
  const sentAt = Date.parse(message.created_at);
  let best: { at: number; by: MemberRef } | null = null;
  for (const escalation of escalations) {
    if (escalation.resolved_at === null || escalation.resolved_by === null) continue;
    const resolvedAt = Date.parse(escalation.resolved_at);
    if (resolvedAt > sentAt) continue;
    if (best === null || resolvedAt > best.at) best = { at: resolvedAt, by: escalation.resolved_by };
  }
  return best?.by ?? null;
}

// Same-instant entries read cause before effect: a decision, its release, then the
// message it produced, any close, the run's end and its reopening; edits and comments last.
const ORDER: Record<TimelineEntry["type"], number> = {
  resolution: 1,
  release: 2,
  message: 3,
  close: 4,
  ended: 5,
  reopen: 6,
  steer: 7,
  note: 8,
};

function compareEntries(a: TimelineEntry, b: TimelineEntry): number {
  const byTime = Date.parse(a.at) - Date.parse(b.at);
  if (byTime !== 0) return byTime;
  const byType = ORDER[a.type] - ORDER[b.type];
  if (byType !== 0) return byType;
  return a.id.localeCompare(b.id);
}

/**
 * How each run ended, from `runs`. A server that doesn't list runs yet, or hasn't
 * recorded the end of the current one, is read from the thread's own fields.
 */
function endings(thread: ThreadDetail): { run: number; at: string; ending: Ending }[] {
  const ended = thread.runs.flatMap(({ run, ended: end }) =>
    end === null
      ? []
      : [{ run, at: end.at, ending: { endReason: end.end_reason, closedBy: end.closed_by, stopReason: end.stop_reason } }],
  );
  if (!isTerminal(thread.state) || ended.some(({ run }) => run === thread.run)) return ended;
  const stopReason = thread.stop_reason ?? null;
  const endReason = thread.end_reason ?? (stopReason === null ? null : "stopped");
  return [
    ...ended,
    {
      run: thread.run,
      at: thread.ended_at ?? thread.last_activity_at,
      ending: { endReason, closedBy: thread.closed_by, stopReason },
    },
  ];
}

/**
 * Reopen rows: each run after the first (opened), joined to our steer when we reopened
 * it, and each of our reopen attempts no run claims (working, or failed). Every reopen
 * steer lands here, so its text shows once.
 */
function reopens(thread: ThreadDetail, steers: readonly SteerView[], us: OrgRef): ReopenEntry[] {
  const reopenSteers = steers.filter((steer) => steer.kind === "reopen");
  const claimed = new Set<string>();
  const entries: ReopenEntry[] = [];
  for (const { run, started_at, reopened } of thread.runs) {
    if (reopened === null) continue;
    const steer = reopenSteers.find((candidate) => candidate.id === reopened.steer_id) ?? null;
    if (steer !== null) claimed.add(steer.id);
    entries.push({
      type: "reopen",
      id: steer === null ? `reopen:run:${String(run)}` : `reopen:${steer.id}`,
      at: started_at,
      org: reopened.org,
      ours: reopened.org.id === us.id,
      by: reopened.by,
      steer,
      outcome: "opened",
    });
  }
  for (const steer of reopenSteers) {
    if (claimed.has(steer.id)) continue;
    entries.push({
      type: "reopen",
      id: `reopen:${steer.id}`,
      at: steer.created_at,
      org: us,
      ours: true,
      by: steer.author,
      steer,
      // A done attempt whose run isn't listed yet has still opened the conversation.
      outcome: steer.status === "done" ? "opened" : steer.status,
    });
  }
  return entries;
}

export function buildTimeline(thread: ThreadDetail, workspace: ThreadWorkspace | undefined): TimelineEntry[] {
  const { us, them } = partiesOf(thread);
  const escalations = workspace?.escalations ?? [];
  const entries: TimelineEntry[] = [];

  const sorted = [...thread.messages].sort((a, b) => a.seq - b.seq);
  for (const message of sorted) {
    const ours = message.from_org_id === us.id;
    const org = ours ? us : them;
    if (message.kind === "close") {
      entries.push({ type: "close", id: message.id, at: message.created_at, ours, org });
      continue;
    }
    entries.push({
      type: "message",
      id: message.id,
      at: message.created_at,
      ours,
      org,
      provenance: message.provenance,
      body: message.body,
      approvedBy: ours ? approvedByOf(message, escalations) : null,
    });
  }

  const latestRun = isTerminal(thread.state) ? thread.run : null;
  for (const { run, at, ending } of endings(thread)) {
    const summary =
      run === latestRun
        ? (workspace?.close_summary ?? null)
        : (workspace?.earlier_summaries.find((earlier) => earlier.run === run)?.text ?? null);
    entries.push({ type: "ended", id: `ended:${String(run)}`, at, run, ending, summary, latest: run === latestRun });
  }
  entries.push(...reopens(thread, workspace?.steers ?? [], us));

  if (workspace === undefined) return entries.sort(compareEntries);

  for (const consult of workspace.consults) {
    if (consult.status !== "released" || consult.released_at === null) continue;
    entries.push({
      type: "release",
      id: `release:${consult.id}`,
      at: consult.released_at,
      consult,
      by: consult.released_by === "member" ? consult.released_by_member : null,
    });
  }

  for (const escalation of escalations) {
    // A consult's approval shows as its release; everything else as a resolution row.
    const releasedConsult =
      escalation.kind === "consult" &&
      escalation.resolution?.action === "approve" &&
      workspace.consults.some((consult) => consult.id === escalation.consult_id && consult.status === "released");
    if (escalation.status !== "resolved" || escalation.resolved_at === null || releasedConsult) continue;
    entries.push({
      type: "resolution",
      id: `resolution:${escalation.id}`,
      at: escalation.resolved_at,
      escalation,
      by: escalation.resolved_by,
    });
  }

  for (const steer of workspace.steers) {
    if (steer.kind === "edit") entries.push({ type: "steer", id: `steer:${steer.id}`, at: steer.created_at, steer });
  }

  for (const note of workspace.notes) {
    entries.push({ type: "note", id: `note:${note.id}`, at: note.created_at, note });
  }

  return entries.sort(compareEntries);
}

/** The open escalation the panel shows: the member's own first, then the oldest. */
export function openEscalation(
  escalations: readonly EscalationView[],
  memberId: string | undefined,
): EscalationView | null {
  const open = escalations.filter((escalation) => escalation.status === "open");
  if (open.length === 0) return null;
  const mine = open.filter((escalation) => escalation.assignees.some((assignee) => assignee.id === memberId));
  const pool = mine.length > 0 ? mine : open;
  return [...pool].sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at))[0] ?? null;
}
