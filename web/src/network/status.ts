/**
 * How participant status and thread state read on the canvas (UI_REFERENCE.md §8,
 * REVISION_UNSTRUCTURED.md §9): pill labels and tones, edge tones and labels, and the
 * header's counts.
 */
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

const EXPIRED: StatusStyle = { label: "Expired", tone: "muted", pulse: false };

/**
 * What an edge says (UI_REFERENCE.md §8): the thread's latest line while it runs, and
 * once it ends the viewer's own side's closing summary, else just that it ended. A
 * thread the viewer can't open has no line, so its color says it alone (null).
 *
 * The line names its sender ("Northlake Characterization Core: Nov 4 works."); the
 * edge has room for little, so a sender among `parties` (the edge's two orgs) is
 * shortened to "Northlake: Nov 4 works.".
 */
export function edgeLabel(edge: NetworkEdge, parties: readonly string[]): string | null {
  if (!EDGE[edge.state].ended) {
    const line = edge.latest_line;
    const sender = parties.find((name) => line?.startsWith(`${name}: `) === true);
    return line === null || sender === undefined ? line : `${shortOrgName(sender)}${line.slice(sender.length)}`;
  }
  return edge.close_summary ?? (edge.state === "expired" ? EXPIRED.label : PARTICIPANT.closed.label);
}

/**
 * A thread's status in the participant vocabulary (REVISION_UNSTRUCTURED.md §9): its
 * recipient's, as the recipient's pill shows it. Without it (a recipient the viewer
 * isn't shown), the thread's own state stands in.
 */
export function threadStatus(state: ThreadState, recipient: ParticipantStatus | null): StatusStyle {
  if (recipient !== null) return PARTICIPANT[recipient];
  switch (state) {
    case "active":
      return PARTICIPANT.messaged;
    case "awaiting_principal":
      return PARTICIPANT.needs_human;
    case "closed":
      return PARTICIPANT.closed;
    case "expired":
      return EXPIRED;
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
