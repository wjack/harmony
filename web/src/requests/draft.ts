/**
 * The decision card's edit model: the member's edit of one draft rev, and the
 * `resolve` bodies built from it. Pure, so what gets sent is unit-tested.
 */
import type { EscalationDraft, EscalationKind, ResolveEscalation } from "./types";

/** What the member changed in the shown draft. Absent: as drafted. */
export interface DraftEdits {
  answer?: string;
}

/** Whether the member's wording differs from Claude's: typing it back as it was isn't an edit. */
export function isEdited(rev: EscalationDraft | null, edits: DraftEdits): boolean {
  return edits.answer !== undefined && edits.answer !== (rev?.answer ?? "");
}

/** The answer (or, for an outbound review, the reply) as it stands after edits. */
export function shownAnswer(rev: EscalationDraft | null, edits: DraftEdits): string {
  return edits.answer ?? rev?.answer ?? "";
}

export type BuildResult = { ok: true; body: ResolveEscalation } | { ok: false; error: string };

/**
 * `approve`: send the answer exactly as shown, edited or not, with the rev it came
 * from. The text always goes with it: the server otherwise takes the newest rev's,
 * which isn't the one shown when the member pages back.
 */
export function approveBody(kind: EscalationKind, rev: EscalationDraft | null, edits: DraftEdits): BuildResult {
  const text = shownAnswer(rev, edits).trim();
  if (text === "") {
    return { ok: false, error: kind === "consult" ? "Write an answer before approving." : "Write the reply before sending it." };
  }
  return { ok: true, body: { action: "approve", text, draft_rev: rev?.draft_rev ?? null } };
}

/** `decline`, with the reason the representative passes on (optional). */
export function declineBody(reason: string, rev: EscalationDraft | null): ResolveEscalation {
  const text = reason.trim();
  return { action: "decline", text: text === "" ? null : text, draft_rev: rev?.draft_rev ?? null };
}

/**
 * Drafts oldest first, so the pager reads "2 of 2" for the newest. A rev that repeats the
 * one before it word for word without a steer (a Retry of the sharing check) stands in
 * for it: there's nothing to page back to.
 */
export function sortedDrafts(drafts: readonly EscalationDraft[]): EscalationDraft[] {
  const sorted = [...drafts].sort((a, b) => a.draft_rev - b.draft_rev);
  return sorted.filter((draft, index) => {
    const next = sorted[index + 1];
    return !(next?.rewritten_by === null && next.answer === draft.answer);
  });
}
