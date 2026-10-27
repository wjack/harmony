/**
 * A request's status and properties (LIVE_DEMO_PLAN 3.9), all derived on the client
 * from the thread and our side's open item: the header's pill and the Details panel.
 */
import type { PillTone } from "../chat/parts";
import { firstName } from "../shell/names";
import { isTerminal, needsLabel, stateLabel } from "./format";
import { partiesOf } from "./timeline";
import type { EscalationView, MemberRef, ThreadDetail } from "./types";

export interface RequestStatus {
  label: string;
  tone: PillTone;
  /** A person is needed now: the pill's dot pulses. */
  needs: boolean;
}

/** Who shared the thread with the viewer, when that's how they can see it. */
function sharedWith(thread: ThreadDetail, memberId: string | undefined): MemberRef | null {
  const mine = thread.access.find((row) => row.member_id === memberId && row.reason === "shared");
  const by = thread.access.find((row) => row.member_id === mine?.shared_by_member_id);
  return by === undefined ? null : { id: by.member_id, slug: by.member_slug, name: by.member_name };
}

/**
 * The pill: an open item first (the viewer's own, else whose it is), then a thread the
 * system stopped, one that ended, one shared with the viewer, and where a live one stands.
 */
export function requestStatus(
  thread: ThreadDetail,
  escalation: EscalationView | null,
  memberId: string | undefined,
): RequestStatus {
  if (escalation !== null) {
    const mine = escalation.assignees.some((assignee) => assignee.id === memberId);
    const who = escalation.assignees.map((assignee) => firstName(assignee.name)).join(", ");
    const label = mine ? needsLabel(escalation.kind) : who === "" ? "Needs a teammate" : `Needs ${who}`;
    return { label, tone: "warning", needs: true };
  }
  if (thread.stop_reason != null) return { label: "Stopped", tone: "muted", needs: false };
  const them = partiesOf(thread).them.name;
  if (isTerminal(thread.state)) {
    return { label: stateLabel(thread, them), tone: thread.state === "closed" ? "success" : "muted", needs: false };
  }
  const sharer = sharedWith(thread, memberId);
  if (sharer !== null) return { label: `Shared by ${firstName(sharer.name)}`, tone: "accent", needs: false };
  return { label: stateLabel(thread, them), tone: "info", needs: false };
}

export interface Properties {
  status: RequestStatus;
  assignees: readonly MemberRef[];
  /** When a live conversation times out (`deadline_at`, the request's lifetime): not a date anyone asked for. */
  expiresAt: string | null;
}

/** The Details panel's facts, from the thread and the open escalation. */
export function propertiesOf(thread: ThreadDetail, escalation: EscalationView | null, memberId: string | undefined): Properties {
  const routed = thread.access
    .filter((access) => access.reason === "routed")
    .map((access) => ({ id: access.member_id, slug: access.member_slug, name: access.member_name }));
  const assignees = escalation !== null && escalation.assignees.length > 0 ? escalation.assignees : routed;
  return {
    status: requestStatus(thread, escalation, memberId),
    assignees,
    expiresAt: isTerminal(thread.state) ? null : thread.deadline_at,
  };
}
