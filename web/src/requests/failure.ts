/**
 * Why Claude didn't come back, in the request view (LIVE_DEMO_PLAN 3.5): a thread the
 * system stopped, and a redraft that failed or never landed. A spent live-model budget
 * fails every retry, so while one is spent the copy says so and shows no Try again; nor
 * does it suggest editing the draft, since approving it would start a turn too.
 */
import type { BudgetState } from "../app/budget";
import { failureLine, type FailureReason } from "../chat/failureCopy";

/** The failure reason a spent budget stands for. */
const SPENT: Record<Exclude<BudgetState, "ok">, FailureReason> = {
  session_exhausted: "session_budget",
  daily_exhausted: "daily_budget",
  demo_exhausted: "demo_budget",
  paused: "paused",
};

export interface RedraftFailure {
  text: string;
  /** Whether asking again could work: not while a budget is spent. */
  retry: boolean;
}

/**
 * A redraft that the worker reported wrote nothing (`failed`), or that never landed
 * within the timeout (`timeout`), given what the budget says now.
 */
export function redraftFailure(how: "failed" | "timeout", state: BudgetState): RedraftFailure {
  if (state !== "ok") {
    return { text: `Claude couldn't redraft this. ${failureLine(SPENT[state], state)}`, retry: false };
  }
  return how === "failed"
    ? { text: "Claude couldn't redraft this. Edit the draft yourself, or try again.", retry: true }
    : { text: "Claude didn't come back with a new draft.", retry: true };
}
