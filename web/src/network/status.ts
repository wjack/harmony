/**
 * How participant status and thread state read on the canvas (UI_REFERENCE.md §8,
 * REVISION_UNSTRUCTURED.md §9): pill labels and tones, edge tones and labels, and the
 * header's counts.
 */
import type { BudgetState } from "../app/budget";
import { reasonLine } from "../requests/format";
import { shortOrgName } from "./format";
import type { NetworkEdge, NetworkNode, ParticipantStatus, ThreadState, WorldLens } from "./types";

/** A color family from the theme tokens (status section of tokens.css). */
export type Tone = "neutral" | "outline" | "info" | "success" | "warning" | "muted";

export interface StatusStyle {
  label: string;
  tone: Tone;
  /** "Needs human" pulses: it's the one status that waits on a person. */
  pulse: boolean;
}

const PARTICIPANT: Record<ParticipantStatus, StatusStyle> = {
  screened: { label: "Screened", tone: "outline", pulse: false },
  messaged: { label: "Messaged", tone: "info", pulse: false },
  replied: { label: "Replied", tone: "success", pulse: false },
  needs_human: { label: "Needs human", tone: "warning", pulse: true },
  closed: { label: "Closed", tone: "muted", pulse: false },
};

export function participantStyle(status: ParticipantStatus): StatusStyle {
  return PARTICIPANT[status];
}

export interface EdgeStyle {
  tone: Tone;
  /** Ended threads are drawn dashed and faint. */
  ended: boolean;
}

const EDGE: Record<ThreadState, EdgeStyle> = {
  active: { tone: "info", ended: false },
  awaiting_principal: { tone: "warning", ended: false },
  closed: { tone: "muted", ended: true },
  expired: { tone: "muted", ended: true },
};

export function edgeStyle(state: ThreadState): EdgeStyle {
  return EDGE[state];
}

/**
 * Why an ended thread ended, naming the org that closed it (shortened) when `parties`
 * (requester first) say who that was.
 */
function endedLine(edge: NetworkEdge, parties: readonly string[], budget: BudgetState): string | null {
  const [requester, responder] = parties.length === 2 ? parties : [];
  const closer = edge.closed_by === "requester" ? requester : edge.closed_by === "responder" ? responder : undefined;
  // A stop always has its reason, even from a server that doesn't say how the run ended.
  const endReason = edge.end_reason ?? (edge.stop_reason ? "stopped" : null);
  return reasonLine(
    { endReason, stopReason: edge.stop_reason ?? null },
    closer === undefined ? null : shortOrgName(closer),
    budget,
  );
}

/**
 * What an ended thread says: the viewer's own side's closing summary, else why it ended
 * (REOPEN.md §2.1), a stop read against the budget's state now. A stopped thread has a
 * summary only when the other side refused: ours is written in the last turn that follows.
 * `parties` as for `edgeLabel`.
 */
export function closingLine(edge: NetworkEdge, parties: readonly string[], budget: BudgetState): string | null {
  const line = edge.close_summary ?? endedLine(edge, parties, budget);
  return line === null ? null : line.charAt(0).toUpperCase() + line.slice(1);
}

/**
 * What an edge says (UI_REFERENCE.md §8): the thread's latest line while it runs, and
 * once it ends the viewer's own side's closing summary, else "Closed: {why}", else just
 * "Closed". A thread the viewer can't open has no line, so its color says it alone (null).
 *
 * `parties` are the edge's two orgs' names, requester first, when the viewer knows
 * both. The line names its sender ("Northlake Characterization Core: Nov 4 works."); the
 * edge has room for little, so a sender among them is shortened to "Northlake: Nov 4
 * works.", and so is the org that closed the thread.
 */
export function edgeLabel(edge: NetworkEdge, parties: readonly string[], budget: BudgetState): string | null {
  if (!EDGE[edge.state].ended) {
    const line = edge.latest_line;
    const sender = parties.find((name) => line?.startsWith(`${name}: `) === true);
    return line === null || sender === undefined ? line : `${shortOrgName(sender)}${line.slice(sender.length)}`;
  }
  if (edge.close_summary !== null) return edge.close_summary;
  const why = endedLine(edge, parties, budget);
  return why === null ? PARTICIPANT.closed.label : `${PARTICIPANT.closed.label}: ${why}`;
}

/**
 * A thread's status in the participant vocabulary (REVISION_UNSTRUCTURED.md §9): its
 * recipient's, as the recipient's pill shows it. Without it (a recipient the viewer
 * isn't shown), the thread's own state stands in, and however it ended it reads Closed.
 */
export function threadStatus(state: ThreadState, recipient: ParticipantStatus | null): StatusStyle {
  if (recipient !== null) return PARTICIPANT[recipient];
  switch (state) {
    case "active":
      return PARTICIPANT.messaged;
    case "awaiting_principal":
      return PARTICIPANT.needs_human;
    case "closed":
    case "expired":
      return PARTICIPANT.closed;
  }
}

/** Stroke color per tone, as theme tokens so dark mode follows. */
export const TONE_STROKE: Record<Tone, string> = {
  neutral: "var(--border-strong)",
  outline: "var(--border-strong)",
  info: "var(--info)",
  success: "var(--success)",
  warning: "var(--warning)",
  muted: "var(--text-faint)",
};

export interface ParticipantCounts {
  /** Recipients whose thread has opened. */
  contacted: number;
  replied: number;
  needsHuman: number;
  closed: number;
  /** Approved recipients whose thread hasn't opened yet. */
  screened: number;
}

/**
 * The header's tallies. Each contacted recipient counts under its current status, the
 * one its pill shows, so "3 replied" excludes a thread that replied and has since
 * closed or come to need a human, and the statuses add up to "contacted".
 */
export function countParticipants(nodes: readonly NetworkNode[]): ParticipantCounts {
  const counts: ParticipantCounts = { contacted: 0, replied: 0, needsHuman: 0, closed: 0, screened: 0 };
  for (const node of nodes) {
    const status = node.participant;
    if (node.role !== "recipient" || status === null) continue;
    if (status === "screened") {
      counts.screened += 1;
      continue;
    }
    counts.contacted += 1;
    if (status === "replied") counts.replied += 1;
    if (status === "needs_human") counts.needsHuman += 1;
    if (status === "closed") counts.closed += 1;
  }
  return counts;
}

/** "5 contacted · 3 replied · 1 needs a human", leaving out the zeros after the first. */
export function countsLine(counts: ParticipantCounts): string[] {
  const parts = [`${counts.contacted} contacted`];
  if (counts.replied > 0) parts.push(`${counts.replied} replied`);
  if (counts.needsHuman > 0) parts.push(counts.needsHuman === 1 ? "1 needs a human" : `${counts.needsHuman} need a human`);
  if (counts.closed > 0) parts.push(`${counts.closed} closed`);
  if (counts.screened > 0) parts.push(`${counts.screened} screened`);
  return parts;
}

/**
 * What the canvas says when nothing is in focus: the world has no request yet, or only
 * finished seeded ones (`earlier`), which the picker lists but the view doesn't open on.
 */
export function noRequestText(world: WorldLens, earlier: boolean): string {
  if (world !== "desk") return "The big run hasn't started in this world yet.";
  return earlier
    ? "Nothing in flight. Ask Claude for something in a new chat, or pick an earlier request above."
    : "No requests yet. Ask Claude for something in a new chat, and the orgs it reaches appear here with where each thread stands.";
}
