/**
 * The lines that say why Claude stopped (LIVE_DEMO_PLAN 3.5): a chat turn
 * (`ConversationDetail.turn_failure`), which the member can try again, or a thread
 * (`stop_reason`), which nothing restarts, given what the budget says now; and the line
 * that says live turns can't start while a cap is spent. The chat, the request view and
 * the network view all read them, so the wording is in one place.
 */
import { useBudget, type BudgetState } from "../app/budget";
import type { components } from "../api/schema";

/** Why the system, not a person, stopped a turn or a thread. */
export type FailureReason = NonNullable<components["schemas"]["ConversationDetail"]["turn_failure"]>;

type Exhausted = Exclude<BudgetState, "ok">;

/** Why a refused call failed, for the state that is still exhausted. */
const EXHAUSTED: Record<Exhausted, string> = {
  session_exhausted:
    "This session has used its live-model budget. Reset doesn't refill it; everything that already happened stays here to explore.",
  daily_exhausted: "Today's live-model budget is used up. It resets at 00:00 UTC.",
  demo_exhausted: "The demo's live-model budget is used up.",
  paused: "Live models are paused right now.",
};

/** A budget refusal while nothing reads exhausted: calls in flight held the rest, or a pause has ended. */
const BUSY = "The live-model budget was busy or nearly used up. Try again in a minute.";

const MODEL_ERROR =
  "Claude couldn't reply because the model service returned an error. Send a message to try again.";
const REFUSED = "Claude declined to respond to this. Try rephrasing.";

/**
 * What to say about a stopped turn or thread. A model error or a refusal reads the same
 * whatever the budget says. A budget refusal reads by the budget's state now: while a
 * cap is spent, that cap's line (the one that also stops the next turn, which may be a
 * different cap from the one that refused this call); once nothing is, the "busy" line,
 * since the next try may well go through.
 */
export function failureLine(reason: FailureReason, state: BudgetState): string {
  if (reason === "model_error") return MODEL_ERROR;
  if (reason === "refused") return REFUSED;
  return state === "ok" ? BUSY : EXHAUSTED[state];
}

type BudgetReason = Exclude<FailureReason, "model_error" | "refused" | "paused">;

/** The cap a budget reason names. */
const CAP: Record<BudgetReason, string> = {
  session_budget: "This session's live-model budget",
  daily_budget: "The daily live-model budget",
  demo_budget: "The demo's live-model budget",
};

/**
 * Why the system ended a thread, after "Closed · {date} ·" on its Closed row. Unlike a
 * chat turn's line it asks for nothing: the row's Reopen is the way back, except after
 * the other side's refusal, when only they can reopen it (REOPEN.md §3.2). A budget
 * reason names its cap: ran out while the budget reads any cap spent, else busy or nearly
 * used up (calls in flight held the rest, or it has refilled since). The budget names
 * only its widest spent cap, so a pause or another spent cap would hide this one's.
 */
export function stoppedLine(reason: FailureReason, state: BudgetState): string {
  switch (reason) {
    case "model_error":
      return "The model service returned an error, so the conversation couldn't go on.";
    case "refused":
      return "Claude declined to continue this conversation.";
    case "paused":
      return "Live models were paused, so the conversation couldn't go on.";
    default: {
      const cap = CAP[reason];
      return state === "ok" ? `${cap} was busy or nearly used up.` : `${cap} ran out.`;
    }
  }
}

const STATE_LINE: Record<Exhausted, string> = {
  session_exhausted: "Session budget used up",
  daily_exhausted: "Today's demo budget is used up",
  demo_exhausted: "The demo's live-model budget is used up",
  paused: "Live models are paused",
};

/** The demo bar's and the composer's line while a cap stops live turns; null while it's `ok`. */
export function budgetLine(state: BudgetState): string | null {
  return state === "ok" ? null : STATE_LINE[state];
}

/**
 * The budget's state now, as `ok` until it loads (or if it can't): the server's own
 * reservation is what refuses a call, so the UI never blocks on a guess.
 */
export function useBudgetState(): BudgetState {
  return useBudget().data?.state ?? "ok";
}
