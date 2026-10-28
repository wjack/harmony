/**
 * The properties rail's activity log ("Claude drafted an answer · 2m"), derived from
 * our side's workspace. Pure, so the wording and order are unit-tested.
 */
import { firstName } from "../shell/names";
import type { EscalationView, MemberRef, ThreadWorkspace } from "./types";

export interface ActivityEntry {
  id: string;
  at: string;
  /** Who acted: a member, or null for Claude. */
  actor: MemberRef | null;
  text: string;
}

/** The item's first entry: why Claude stopped for a teammate. */
const FLAGGED: Readonly<Record<EscalationView["kind"], string>> = {
  consult: "Claude asked a teammate to check the answer",
  outbound_review: "Claude held a reply for review",
  refused_turn: "Claude declined to continue the conversation",
};

function resolutionText(escalation: EscalationView): string {
  if (escalation.kind === "refused_turn") {
    return escalation.resolution?.action === "decline" ? "ended the conversation" : "asked Claude to try again";
  }
  switch (escalation.resolution?.action) {
    case "approve":
      return escalation.kind === "consult" ? "released the answer to this thread" : "approved the reply";
    case "decline":
      return escalation.kind === "consult" ? "declined to answer" : "declined the reply";
    case "take_over":
      return "replied directly";
    default:
      return "resolved the item";
  }
}

export function buildActivity(workspace: ThreadWorkspace): ActivityEntry[] {
  const entries: ActivityEntry[] = [];

  for (const consult of workspace.consults) {
    if (consult.status === "released" && consult.released_at !== null && consult.released_by === "auto") {
      entries.push({ id: `auto:${consult.id}`, at: consult.released_at, actor: null, text: "Claude answered without a teammate" });
    }
  }

  for (const escalation of workspace.escalations) {
    const what = escalation.kind === "consult" ? "an answer" : "a reply";
    entries.push({
      id: `flagged:${escalation.id}`,
      at: escalation.created_at,
      actor: null,
      text: FLAGGED[escalation.kind],
    });
    for (const draft of escalation.drafts) {
      if (draft.draft_rev === 1) continue;
      const rev = `(rev ${String(draft.draft_rev)})`;
      entries.push({
        id: `draft:${escalation.id}:${String(draft.draft_rev)}`,
        at: draft.created_at,
        actor: null,
        text:
          draft.rewritten_by === null
            ? `Claude redrafted ${what} ${rev}`
            : `Claude rewrote the draft from ${firstName(draft.rewritten_by.author.name)}'s edit ${rev}`,
      });
    }
    if (escalation.status === "resolved" && escalation.resolved_at !== null) {
      entries.push({
        id: `resolved:${escalation.id}`,
        at: escalation.resolved_at,
        actor: escalation.resolved_by,
        text: resolutionText(escalation),
      });
    }
    if (escalation.status === "cancelled") {
      entries.push({
        id: `cancelled:${escalation.id}`,
        at: escalation.resolved_at ?? escalation.created_at,
        actor: null,
        text: "The item was withdrawn",
      });
    }
  }

  for (const steer of workspace.steers) {
    const text = steer.kind === "edit" ? "told Claude what to do differently" : "reopened the conversation";
    // A reopen that didn't go through changed nothing anyone else saw.
    if (steer.kind === "reopen" && steer.status !== "done") continue;
    entries.push({ id: `steer:${steer.id}`, at: steer.created_at, actor: steer.author, text });
  }

  for (const note of workspace.notes) {
    entries.push({
      id: `note:${note.id}`,
      at: note.created_at,
      actor: note.author_kind === "member" ? note.author : null,
      text: note.author_kind === "member" ? "added a comment" : "Claude added a note",
    });
  }

  return entries.sort((a, b) => Date.parse(a.at) - Date.parse(b.at) || a.id.localeCompare(b.id));
}
