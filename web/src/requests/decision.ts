/**
 * What the decision card says about an open item (EDIT.md §8.3, composer.html): whose it
 * is, what it asks, why it waits (or what Claude declined), what approving and declining
 * do, and how the held item's org is set. Anyone who can see the item may act on it, so the words name the
 * assignee when it isn't the viewer. Pure, so the wording is unit-tested.
 */
import { declinedLine } from "../chat/failureCopy";
import { firstName } from "../shell/names";
import { possessive } from "./format";
import type { ConsultView, EscalationDraft, EscalationView, MemberRef, OrgRef } from "./types";

export type Refusal = NonNullable<EscalationView["refusal"]>;

/** The longest answer or message the server takes with a resolution. */
export const ANSWER_MAX_CHARS = 20_000;

/** Customize › Harmony › Autonomy, where "Ask mode with {org} · Change" goes. */
export const AUTONOMY_PATH = "/customize/harmony?section=autonomy";

function isAssignee(escalation: EscalationView, memberId: string | undefined): boolean {
  return escalation.assignees.some((assignee) => assignee.id === memberId);
}

/** "Raj", "Helen or Raj": either can answer. */
function either(assignees: readonly MemberRef[]): string {
  const names = assignees.map((assignee) => firstName(assignee.name));
  if (names.length === 0) return "a teammate";
  return names.length === 1 ? (names[0] ?? "") : `${names.slice(0, -1).join(", ")} or ${names.at(-1) ?? ""}`;
}

/** The card's first line: what Claude is waiting for, and from whom. */
export function eyebrow(escalation: EscalationView, them: OrgRef, memberId: string | undefined): string {
  const mine = isAssignee(escalation, memberId);
  if (escalation.kind === "consult") {
    const whose = mine ? "your" : possessive(either(escalation.assignees));
    return `Claude needs ${whose} answer before replying to ${them.name}`;
  }
  if (escalation.kind === "refused_turn") {
    const who = mine ? "you" : either(escalation.assignees);
    return `Claude stopped before replying to ${them.name} · ${who} can retry or end it`;
  }
  const wants = `Claude wants to send this to ${them.name}`;
  return mine ? wants : `${wants} · ${either(escalation.assignees)} reviews`;
}

/**
 * The card's title. A consult reads as the answerer's short question (the shown draft's,
 * else the newest draft's); a draft without one, from before short questions or a
 * failed answerer, falls back to the representative's whole question.
 */
export function cardTitle(
  escalation: EscalationView,
  shown: EscalationDraft | null,
  consult: ConsultView | null,
  them: OrgRef,
): string {
  if (escalation.kind === "outbound_review") return `Send this reply to ${them.name}?`;
  const newest = [...escalation.drafts]
    .sort((a, b) => b.draft_rev - a.draft_rev)
    .find((draft) => (draft.short_question ?? "") !== "");
  const short = (shown?.short_question ?? "") === "" ? newest?.short_question : shown?.short_question;
  return short ?? consult?.question ?? "Claude needs an answer before it replies";
}

/** Why an item waits when nothing more specific stopped it: Ask mode, which the foot already says. */
const ASK_MODE_REASONS: ReadonlySet<string> = new Set([
  "A teammate approves every message in Ask mode.",
  "A teammate approves every answer in Ask mode.",
]);

/**
 * The muted line under the title: why this item needs a person (the answerer's or the
 * sharing check's reason, or a held message's), or null when it's only Ask mode.
 */
export function reasonLine(escalation: EscalationView): string | null {
  const reason = escalation.reason.trim();
  return reason === "" || ASK_MODE_REASONS.has(reason) ? null : reason;
}

/** The note in the reason's place when Claude declined the step the item waits on. */
export function refusalLine(refusal: Refusal): string {
  switch (refusal.step) {
    case "answer":
      return declinedLine("draft an answer", refusal.category);
    case "sharing_check":
      return declinedLine("check this answer against what you share", refusal.category);
    case "turn":
      return declinedLine("continue the conversation", refusal.category);
  }
}

/** The draft part's name. */
export function draftLabel(escalation: EscalationView): string {
  return escalation.kind === "consult" ? "Draft reply" : "Claude's message";
}

/** What approving does, under Approve and send. */
export function approveHint(escalation: EscalationView, them: OrgRef, edited: boolean): string {
  if (escalation.kind === "outbound_review") return `Sends this message to ${them.name} as shown`;
  return `Claude replies to ${them.name} with ${edited ? "your edited answer" : "this answer"}`;
}

/** The Decline step's heading and who may see the reason. */
export function declineCopy(escalation: EscalationView, them: OrgRef): { title: string; caption: string } {
  if (escalation.kind === "consult") {
    return {
      title: "Decline to answer",
      caption: `Claude tells ${them.name} you can't answer, and may pass on your reason.`,
    };
  }
  return {
    title: "Decline this message",
    caption: `Claude won't send this message. It may write another, which comes back to you first. ${them.name} may see your reason.`,
  };
}

/** "Ask mode with Vantage Market Intelligence", from the mode's label (`MODE_INFO`). */
export function modeLine(label: string, them: OrgRef): string {
  return `${label} mode with ${them.name}`;
}

/** The link to what our side shares with them: admins edit it, everyone else reads it. */
export function sharingLabel(admin: boolean, them: OrgRef): string {
  return `${admin ? "Edit" : "See"} what you share with ${them.name}`;
}

/**
 * "Drafted from Alex's answer to Ridgeline, Oct 26": the member's answer in another
 * conversation that a reused draft started from, worded by the server for this viewer.
 * Null when the draft didn't start from one.
 */
export function draftedFrom(consult: ConsultView | null): string | null {
  return consult?.drafted_from ?? null;
}

/** The items Claude read to draft, as one line: a record's title, else what it searched for. */
export function lookedAtSummary(rev: EscalationDraft): string {
  return rev.activity.map((line) => (line.detail == null || line.detail === "" ? line.label : line.detail)).join(" · ");
}
