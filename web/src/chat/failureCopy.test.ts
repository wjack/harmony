import { describe, expect, it } from "vitest";

import type { BudgetState } from "../app/budget";
import { budgetLine, declinedLine, failureLine, stoppedLine, type FailureReason } from "./failureCopy";

const STATES: readonly BudgetState[] = ["ok", "demo_exhausted", "paused"];

const BUSY = "Live models were stopped for a moment. Try again in a minute.";

/** Each stopped state's line (LIVE_DEMO_PLAN 3.5), which also says why the next turn can't start. */
const SPENT: Record<Exclude<BudgetState, "ok">, string> = {
  demo_exhausted: "The demo's live-model budget is used up.",
  paused: "Live models are paused right now.",
};

const BUDGET_REASONS: readonly FailureReason[] = ["demo_budget", "paused"];

describe("failureLine", () => {
  const cases = BUDGET_REASONS.flatMap((reason) => STATES.map((state) => [reason, state] as const));

  it.each(cases)("says why a %s stop happened while the budget is %s", (reason, state) => {
    expect(failureLine(reason, state)).toBe(state === "ok" ? BUSY : SPENT[state]);
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

  it("says the demo's budget ran out while live turns are stopped, else busy", () => {
    expect(stoppedLine("demo_budget", "demo_exhausted")).toBe("The demo's live-model budget ran out.");
    expect(stoppedLine("demo_budget", "ok")).toBe("The demo's live-model budget ran out for a while.");
    // A pause hides the spent budget that stopped the thread.
    expect(stoppedLine("demo_budget", "paused")).toBe("The demo's live-model budget ran out.");
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
    expect(STATES.map(budgetLine)).toEqual([null, "The demo's live-model budget is used up", "Live models are paused"]);
  });
});
