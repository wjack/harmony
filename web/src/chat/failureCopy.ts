/**
 * The lines that say why Claude stopped (LIVE_DEMO_PLAN 3.5): a chat turn
 * (`ConversationDetail.turn_failure`), which the member can try again, or a thread
 * (`stop_reason`), which nothing restarts, given what the budget says now; and the line
 * that says live turns can't start while live models are paused or the demo's budget is
 * spent. The chat, the request view and the network view all read them, so the wording
 * is in one place.
 */
import { useBudget, type BudgetState } from "../app/budget";
import type { components } from "../api/schema";

/** Why the system, not a person, stopped a turn or a thread. */
export type FailureReason = NonNullable<components["schemas"]["ConversationDetail"]["turn_failure"]>;

type Exhausted = Exclude<BudgetState, "ok">;

/** Why a refused call failed, for the state that is still exhausted. */
const EXHAUSTED: Record<Exhausted, string> = {
  demo_exhausted: "The demo's live-model budget is used up.",
  paused: "Live models are paused right now.",
};

/** A budget refusal while nothing reads exhausted: a pause or a provider block has ended since. */
const BUSY = "Live models were stopped for a moment. Try again in a minute.";

const MODEL_ERROR =
  "Claude couldn't reply because the model service returned an error. Send a message to try again.";

/** The API's refusal categories (`stop_details.category`) as the notes name them; others show as given. */
const CATEGORY: Readonly<Record<string, string>> = { bio: "biosecurity", cyber: "cybersecurity" };

/**
 * A note that Claude declined a step, naming the category when the API gave one:
 * `declinedLine("respond to this", "bio")` is "Claude declined to respond to this (biosecurity)."
 */
export function declinedLine(what: string, category: string | null): string {
  if (category === null) return `Claude declined to ${what}.`;
  return `Claude declined to ${what} (${CATEGORY[category] ?? category}).`;
}

/**
 * What to say about a stopped turn or thread. A model error or a refusal (with its
 * `category`, if any) reads the same whatever the budget says. A budget refusal or a
 * pause reads by the budget's state now: while live turns are stopped, that state's line
 * (the one that also stops the next turn); once they aren't, the "for a moment" line,
 * since the next try may well go through.
 */
export function failureLine(reason: FailureReason, state: BudgetState, category: string | null = null): string {
  if (reason === "model_error") return MODEL_ERROR;
  if (reason === "refused") return `${declinedLine("respond to this", category)} Try rephrasing.`;
  return state === "ok" ? BUSY : EXHAUSTED[state];
}

/**
 * Why the system ended a thread, after "Closed · {date} ·" on its Closed row. Unlike a
 * chat turn's line it asks for nothing: the row's Reopen is the way back, except after
 * the other side's refusal, when only they can reopen it (REOPEN.md §3.2). The demo's
 * budget ran out while the budget reads anything but `ok` (a pause hides it), else it
 * ran out for a while (the provider block has ended since).
 */
export function stoppedLine(reason: FailureReason, state: BudgetState): string {
  switch (reason) {
    case "model_error":
      return "The model service returned an error, so the conversation couldn't go on.";
    case "refused":
      return "Claude declined to continue this conversation.";
    case "paused":
      return "Live models were paused, so the conversation couldn't go on.";
    case "demo_budget":
      return state === "ok"
        ? "The demo's live-model budget ran out for a while."
        : "The demo's live-model budget ran out.";
  }
}

const STATE_LINE: Record<Exhausted, string> = {
  demo_exhausted: "The demo's live-model budget is used up",
  paused: "Live models are paused",
};

/** The demo bar's and the composer's line while live turns are stopped; null while it's `ok`. */
export function budgetLine(state: BudgetState): string | null {
  return state === "ok" ? null : STATE_LINE[state];
}

/**
 * The budget's state now, as `ok` until it loads (or if it can't): the server's own
 * check is what refuses a call, so the UI never blocks on a guess.
 */
export function useBudgetState(): BudgetState {
  return useBudget().data?.state ?? "ok";
}
