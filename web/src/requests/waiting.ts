/**
 * The transcript's steady last row (turn_states.html, decision 2): whose move it is
 * while nothing of ours is being written. The other side never shows as writing: from
 * the moment our message lands until theirs does, the row reads "Waiting for {them} to
 * reply", whatever their Claude is doing. On our move it says what our side is doing,
 * when that isn't already on screen. Pure, so the rules are unit-tested.
 */
import { isTerminal, possessive, type Standing } from "./format";
import type { ConsultView, EscalationView, OrgRef } from "./types";

export interface WaitingInput {
  thread: Standing;
  /** Our Claude is writing to them now: its row says what's happening. */
  typing: boolean;
  consults: readonly ConsultView[];
  /** The open item the view shows a card for. */
  escalation: EscalationView | null;
  viewerId: string | undefined;
  us: OrgRef;
  them: OrgRef;
}

export function waitingLine({ thread, typing, consults, escalation, viewerId, us, them }: WaitingInput): string | null {
  if (isTerminal(thread.state) || typing) return null;
  if (thread.side_to_move !== null && thread.side_to_move !== thread.side) return `Waiting for ${them.name} to reply`;
  if (escalation !== null) {
    // The assignee has the card; everyone else is told whose answer it waits on.
    if (escalation.assignees.some((assignee) => assignee.id === viewerId)) return null;
    const names = escalation.assignees.map((assignee) => assignee.name);
    return names.length === 0 ? "Waiting on a teammate" : `Waiting on ${names.join(" and ")}`;
  }
  if (consults.some((consult) => consult.status === "answering")) return `Checking ${possessive(us.name)} records…`;
  return null;
}
