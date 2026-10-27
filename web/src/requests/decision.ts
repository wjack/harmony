/**
 * What the decision card and the composer say about an open item (LIVE_DEMO_PLAN 3.9):
 * whose it is, what it asks, why it came to the viewer and what approving does. Anyone
 * who can see the item may act on it, so the words name the assignee when it isn't the
 * viewer. Pure, so the wording is unit-tested.
 */
import { firstName } from "../shell/names";
import { possessive } from "./format";
import type { ConsultView, EscalationDraft, EscalationView, MemberRef, OrgRef } from "./types";

function isAssignee(escalation: EscalationView, memberId: string | undefined): boolean {
  return escalation.assignees.some((assignee) => assignee.id === memberId);
}

/** "Raj", "Helen or Raj": either can answer. */
function either(assignees: readonly MemberRef[]): string {
  const names = assignees.map((assignee) => firstName(assignee.name));
  if (names.length === 0) return "a teammate";
  return names.length === 1 ? (names[0] ?? "") : `${names.slice(0, -1).join(", ")} or ${names.at(-1) ?? ""}`;
}

/** "Helen", "Helen and Maya": who else has it. */
function both(assignees: readonly MemberRef[]): string {
  const names = assignees.map((assignee) => firstName(assignee.name));
  return names.length <= 1 ? (names[0] ?? "") : `${names.slice(0, -1).join(", ")} and ${names.at(-1) ?? ""}`;
}

/** The card's first line: what Claude is waiting for, and from whom. */
export function eyebrow(escalation: EscalationView, them: OrgRef, memberId: string | undefined): string {
  const mine = isAssignee(escalation, memberId);
  if (escalation.kind === "consult") {
    const whose = mine ? "your" : possessive(either(escalation.assignees));
    return `Claude needs ${whose} answer before replying to ${them.name}`;
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

/**
 * The draft the item opened with, whose route routing applied. A redraft never routes
 * again, and an item opened without a draft (the answerer failed) has no rev 1.
 */
function openingDraft(escalation: EscalationView): EscalationDraft | null {
  return escalation.drafts.find((draft) => draft.draft_rev === 1) ?? null;
}

/** How the item reached the viewer, for "Why it came to you". */
export interface Routing {
  /** "Routed to you: you run XRD and SEM", "Assigned to Raj". */
  line: string;
  /** "with Helen", when routing gave it to others as well. */
  with: string | null;
}

/**
 * While the item is with the assignees Claude routed it to: the viewer's own why (or the
 * first assignee's, for someone else's item) and who else has it. An item whose route
 * named no one, and every held message, went to the org's default assignee
 * (`defaultAssigneeId`, the org's setting; undefined until it has loaded), and says so
 * while it is still with them. One a teammate moved says only who has it now. The API
 * gives the route as code applied it (`routing.named_slugs`): its own members' slugs,
 * unknown names dropped.
 */
export function routingOf(
  escalation: EscalationView,
  memberId: string | undefined,
  us: OrgRef,
  defaultAssigneeId: string | null | undefined,
): Routing {
  const mine = isAssignee(escalation, memberId);
  const assigned: Routing = { line: `Assigned to ${mine ? "you" : both(escalation.assignees)}`, with: null };
  const whys = new Map((openingDraft(escalation)?.route?.assignees ?? []).map((named) => [named.slug, named.why]));
  if (whys.size === 0) {
    const [holder, ...others] = escalation.assignees;
    if (holder === undefined || others.length > 0 || holder.id !== defaultAssigneeId) return assigned;
    const to = mine ? "you" : firstName(holder.name);
    return { line: `Sent to ${to} as ${possessive(us.name)} default assignee.`, with: null };
  }
  const asRouted =
    escalation.assignees.length > 0 && escalation.assignees.every((assignee) => whys.has(assignee.slug));
  if (!asRouted) return assigned;
  const reader = escalation.assignees.find((assignee) => assignee.id === memberId) ?? escalation.assignees[0];
  const why = (reader === undefined ? undefined : whys.get(reader.slug)) ?? "";
  const to = mine || reader === undefined ? "you" : firstName(reader.name);
  const others = escalation.assignees.filter((assignee) => assignee.id !== reader?.id);
  // The why is addressed to its assignee ("you run XRD"): quoted when someone else reads it.
  const said = to === "you" ? why : `“${why}”`;
  return {
    line: why === "" ? `Routed to ${to}` : `Routed to ${to}: ${said}`,
    with: others.length === 0 ? null : `with ${both(others)}`,
  };
}

/** The heading of the card's routing part: "Why it came to you", or to whoever has it. */
export function whyHeading(escalation: EscalationView, memberId: string | undefined): string {
  if (isAssignee(escalation, memberId) || escalation.assignees.length === 0) return "Why it came to you";
  return `Why it came to ${both(escalation.assignees)}`;
}

/**
 * The line under the composer: what approving does, and for whom when it isn't the
 * viewer's. ⌘↵ approves only from an empty composer; with text typed it redrafts or
 * sends the message back, so the hint goes.
 */
export function approvalCaption(
  escalation: EscalationView,
  them: OrgRef,
  memberId: string | undefined,
  typed: boolean,
): string {
  if (isAssignee(escalation, memberId)) {
    const sends = `Approving sends this reply to ${them.name} only`;
    return typed ? sends : `${sends} · ⌘↵ to approve`;
  }
  return `Approving answers for ${either(escalation.assignees)} · sends this reply to ${them.name} only`;
}

/** The items Claude read to draft, as one line: a record's title, else what it searched for. */
export function lookedAtSummary(rev: EscalationDraft): string {
  return rev.activity.map((line) => (line.detail == null || line.detail === "" ? line.label : line.detail)).join(" · ");
}
