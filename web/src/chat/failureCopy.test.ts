import { describe, expect, it } from "vitest";

import type { BudgetState } from "../app/budget";
import { budgetLine, declinedLine, failureLine, stoppedLine, type FailureReason } from "./failureCopy";

const STATES: readonly BudgetState[] = ["ok", "session_exhausted", "daily_exhausted", "demo_exhausted", "paused"];

const BUSY = "The live-model budget was busy or nearly used up. Try again in a minute.";

/** Each spent cap's line (LIVE_DEMO_PLAN 3.5), which also says why the next turn can't start. */
const SPENT: Record<Exclude<BudgetState, "ok">, string> = {
  session_exhausted:
    "This session has used its live-model budget. Reset doesn't refill it; everything that already happened stays here to explore.",
  daily_exhausted: "Today's live-model budget is used up. It resets at 00:00 UTC.",
  demo_exhausted: "The demo's live-model budget is used up.",
  paused: "Live models are paused right now.",
};

const BUDGET_REASONS: readonly FailureReason[] = ["session_budget", "daily_budget", "demo_budget", "paused"];

describe("failureLine", () => {
  const cases = BUDGET_REASONS.flatMap((reason) => STATES.map((state) => [reason, state] as const));

  it.each(cases)("says why a %s stop happened while the budget is %s", (reason, state) => {
    expect(failureLine(reason, state)).toBe(state === "ok" ? BUSY : SPENT[state]);
  });

  it("matches each budget reason to its own cap's line", () => {
    expect(failureLine("session_budget", "session_exhausted")).toBe(SPENT.session_exhausted);
    expect(failureLine("daily_budget", "daily_exhausted")).toBe(SPENT.daily_exhausted);
    expect(failureLine("demo_budget", "demo_exhausted")).toBe(SPENT.demo_exhausted);
    expect(failureLine("paused", "paused")).toBe(SPENT.paused);
  });

  it("reads a session stop or a pause as busy once the budget is ok again", () => {
    expect(failureLine("session_budget", "ok")).toBe(BUSY);
    expect(failureLine("paused", "ok")).toBe(BUSY);
  });

  it.each(STATES)("says the same about a model error or a refusal whatever the budget (%s)", (state) => {
    expect(failureLine("model_error", state)).toBe(
      "Claude couldn't reply because the model service returned an error. Send a message to try again.",
    );
    expect(failureLine("refused", state)).toBe("Claude declined to respond to this. Try rephrasing.");
    expect(failureLine("refused", state, "bio")).toBe("Claude declined to respond to this (biosecurity). Try rephrasing.");
  });
});

describe("declinedLine", () => {
  it("names the API's category in words, and any other as given", () => {
    expect(declinedLine("draft an answer", null)).toBe("Claude declined to draft an answer.");
    expect(declinedLine("draft an answer", "cyber")).toBe("Claude declined to draft an answer (cybersecurity).");
    expect(declinedLine("respond to this", "bio")).toBe("Claude declined to respond to this (biosecurity).");
    expect(declinedLine("respond to this", "weapons")).toBe("Claude declined to respond to this (weapons).");
  });
});

describe("stoppedLine", () => {
  const REASONS: readonly FailureReason[] = [...BUDGET_REASONS, "model_error", "refused"];
  const cases = REASONS.flatMap((reason) => STATES.map((state) => [reason, state] as const));

  it.each(cases)("asks for no retry on a thread a %s stopped, budget %s", (reason, state) => {
    expect(stoppedLine(reason, state)).not.toMatch(/try again|try rephrasing|send a message/i);
  });

  it("names the cap that stopped it: ran out while any cap reads spent, else busy", () => {
    expect(stoppedLine("session_budget", "session_exhausted")).toBe("This session's live-model budget ran out.");
    expect(stoppedLine("daily_budget", "daily_exhausted")).toBe("The daily live-model budget ran out.");
    expect(stoppedLine("demo_budget", "demo_exhausted")).toBe("The demo's live-model budget ran out.");
    expect(stoppedLine("demo_budget", "ok")).toBe("The demo's live-model budget was busy or nearly used up.");
    // The budget names only its widest spent cap: a pause, or the session's cap spent
    // too, hides the one that stopped the thread, which is still spent.
    expect(stoppedLine("session_budget", "paused")).toBe("This session's live-model budget ran out.");
    expect(stoppedLine("daily_budget", "session_exhausted")).toBe("The daily live-model budget ran out.");
  });

  it.each(STATES)("says what stopped it otherwise, whatever the budget (%s)", (state) => {
    expect(stoppedLine("paused", state)).toBe("Live models were paused, so the conversation couldn't go on.");
    expect(stoppedLine("model_error", state)).toBe(
      "The model service returned an error, so the conversation couldn't go on.",
    );
    expect(stoppedLine("refused", state)).toBe("Claude declined to continue this conversation.");
  });
});

describe("budgetLine", () => {
  it("is null while the budget is ok, and the state's line otherwise", () => {
    expect(STATES.map(budgetLine)).toEqual([
      null,
      "Session budget used up",
      "Today's demo budget is used up",
      "The demo's live-model budget is used up",
      "Live models are paused",
    ]);
  });
});
