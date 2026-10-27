/**
 * The properties rail's activity log ("Claude drafted an answer · 2m"), derived from
 * our side's workspace. Pure, so the wording and order are unit-tested.
 */
import type { EscalationView, MemberRef, OrgRef, ThreadWorkspace } from "./types";

export interface ActivityEntry {
  id: string;
  at: string;
  /** Who acted: a member, or null for Claude. */
  actor: MemberRef | null;
  text: string;
}

function resolutionText(escalation: EscalationView): string {
  switch (escalation.resolution?.action) {
    case "approve":
      return escalation.kind === "consult" ? "released the answer to this thread" : "approved the reply";
    case "decline":
      return escalation.kind === "consult" ? "declined to answer" : "declined the reply";
    case "take_over":
      return "replied directly";
    case "revise":
      return "sent the reply back to Claude";
    case undefined:
      return "resolved the item";
  }
}

export function buildActivity(workspace: ThreadWorkspace, us: OrgRef): ActivityEntry[] {
  const entries: ActivityEntry[] = [];
  const rep = `Claude for ${us.name}`;

  for (const consult of workspace.consults) {
    entries.push({ id: `consult:${consult.id}`, at: consult.created_at, actor: null, text: `${rep} asked the team a question` });
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
      text: escalation.kind === "consult" ? "Claude asked a teammate to check the answer" : "Claude held a reply for review",
    });
    for (const draft of escalation.drafts) {
      if (draft.draft_rev === 1) continue;
      entries.push({
        id: `draft:${escalation.id}:${String(draft.draft_rev)}`,
        at: draft.created_at,
        actor: null,
        text: `Claude redrafted ${what} (rev ${String(draft.draft_rev)})`,
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

  for (const note of workspace.notes) {
    entries.push({
      id: `note:${note.id}`,
      at: note.created_at,
      actor: note.author_kind === "member" ? note.author : null,
      text: note.author_kind === "member" ? "added an internal note" : "Claude added an internal note",
    });
  }

  return entries.sort((a, b) => Date.parse(a.at) - Date.parse(b.at) || a.id.localeCompare(b.id));
}
