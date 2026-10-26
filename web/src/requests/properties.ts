/** The properties rail's facts, derived from the thread and our side's open item. */
import { firstName } from "../shell/names";
import { isTerminal, stateLabel } from "./format";
import { partiesOf } from "./timeline";
import type { EscalationView, MemberRef, ThreadDetail } from "./types";

export interface Properties {
  status: { label: string; tone: "needs" | "live" | "done" | "ended" };
  assignees: readonly MemberRef[];
  /** When a live conversation times out (`deadline_at`, the request's lifetime): not a date anyone asked for. */
  expiresAt: string | null;
}

/** The rail's facts, from the thread and the open escalation. */
export function propertiesOf(thread: ThreadDetail, escalation: EscalationView | null, memberId: string | undefined): Properties {
  let status: Properties["status"];
  if (escalation !== null) {
    const mine = escalation.assignees.some((assignee) => assignee.id === memberId);
    const who = mine ? "you" : escalation.assignees.map((assignee) => firstName(assignee.name)).join(", ");
    status = { label: who === "" ? "Needs a teammate" : `Needs ${who}`, tone: "needs" };
  } else if (isTerminal(thread.state)) {
    status = { label: stateLabel(thread, partiesOf(thread).them.name), tone: thread.state === "closed" ? "done" : "ended" };
  } else {
    status = { label: stateLabel(thread, partiesOf(thread).them.name), tone: "live" };
  }
  const routed = thread.access
    .filter((access) => access.reason === "routed")
    .map((access) => ({ id: access.member_id, slug: access.member_slug, name: access.member_name }));
  const assignees = escalation !== null && escalation.assignees.length > 0 ? escalation.assignees : routed;
  return { status, assignees, expiresAt: isTerminal(thread.state) ? null : thread.deadline_at };
}
