/**
 * The thread view's transcript: what crossed the network (both sides see it) merged
 * with what happened on our side of the firewall (consults, releases, resolutions and
 * internal notes), in time order. Pure, so the ordering rules are unit-tested.
 */
import type {
  ConsultView,
  EscalationDraft,
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

export interface ConsultEntry {
  type: "consult";
  id: string;
  at: string;
  consult: ConsultView;
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

export interface NoteEntry {
  type: "note";
  id: string;
  at: string;
  note: NoteView;
}

export type TimelineEntry = MessageEntry | CloseEntry | ConsultEntry | ReleaseEntry | ResolutionEntry | NoteEntry;

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

// Same-instant entries read cause before effect: a question, its release, then the
// message it produced and any close; notes last.
const ORDER: Record<TimelineEntry["type"], number> = {
  consult: 0,
  resolution: 1,
  release: 2,
  message: 3,
  close: 4,
  note: 5,
};

function compareEntries(a: TimelineEntry, b: TimelineEntry): number {
  const byTime = Date.parse(a.at) - Date.parse(b.at);
  if (byTime !== 0) return byTime;
  const byType = ORDER[a.type] - ORDER[b.type];
  if (byType !== 0) return byType;
  return a.id.localeCompare(b.id);
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

  if (workspace === undefined) return entries;

  for (const consult of workspace.consults) {
    entries.push({ type: "consult", id: `consult:${consult.id}`, at: consult.created_at, consult });
    if (consult.status === "released" && consult.released_at !== null) {
      entries.push({
        type: "release",
        id: `release:${consult.id}`,
        at: consult.released_at,
        consult,
        by: consult.released_by === "member" ? consult.released_by_member : null,
      });
    }
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

/** The draft the item shows by default: its current rev, else the newest. */
export function currentDraft(escalation: EscalationView): EscalationDraft | null {
  const drafts = [...escalation.drafts].sort((a, b) => b.draft_rev - a.draft_rev);
  return drafts.find((draft) => draft.draft_rev === escalation.current_draft_rev) ?? drafts[0] ?? null;
}

/**
 * The draft the item opened with, whose route routing applied. A redraft never routes
 * again, and an item opened without a draft (the answerer failed) has no rev 1.
 */
function openingDraft(escalation: EscalationView): EscalationDraft | null {
  return escalation.drafts.find((draft) => draft.draft_rev === 1) ?? null;
}

/**
 * "Routed to you by Claude: <reason>" while the item is with the assignees routing gave
 * it from Claude's opening draft; for a question that named no one on the team, that it
 * first went to the default assignee (still true once moved: a move records nothing);
 * otherwise "Assigned to Raj" (a teammate moved it, or a held message). The API gives
 * the route as code applied it (`routing.named_slugs`): its own members' slugs,
 * unknown names dropped.
 */
export function routedSentence(
  escalation: EscalationView,
  memberId: string | undefined,
  firstNameOf: (name: string) => string,
): string {
  const mine = escalation.assignees.some((assignee) => assignee.id === memberId);
  const to = mine ? "you" : escalation.assignees.map((assignee) => firstNameOf(assignee.name)).join(", ") || "the team";
  const route = openingDraft(escalation)?.route ?? null;
  const named = new Set(route?.assignees ?? []);
  if (escalation.kind === "consult" && named.size === 0) {
    return `Assigned to ${to}. Claude named no one on the team, so it first went to the default assignee`;
  }
  const asRouted =
    route !== null &&
    escalation.assignees.length > 0 &&
    escalation.assignees.every((assignee) => named.has(assignee.slug));
  if (!asRouted) return `Assigned to ${to}`;
  return route.reason === "" ? `Routed to ${to} by Claude` : `Routed to ${to} by Claude: ${route.reason}`;
}
