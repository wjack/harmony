import { describe, expect, it } from "vitest";

import { failureLine } from "../chat/failureCopy";
import { redraftFailure } from "./failure";

describe("redraftFailure", () => {
  it("shows Try again while the budget allows one", () => {
    expect(redraftFailure("failed", "ok")).toEqual({
      text: "Claude couldn't redraft this. Edit the draft yourself, or try again.",
      retry: true,
    });
    expect(redraftFailure("timeout", "ok")).toEqual({ text: "Claude didn't come back with a new draft.", retry: true });
  });

  it("says which budget is spent instead of showing a retry that would fail", () => {
    expect(redraftFailure("failed", "demo_exhausted")).toEqual({
      text: `Claude couldn't redraft this. ${failureLine("demo_budget", "demo_exhausted")}`,
      retry: false,
    });
    expect(redraftFailure("timeout", "paused").text).toBe(
      `Claude couldn't redraft this. ${failureLine("paused", "paused")}`,
    );
    expect(redraftFailure("timeout", "session_exhausted").retry).toBe(false);
    expect(redraftFailure("failed", "daily_exhausted").text).toContain(failureLine("daily_budget", "daily_exhausted"));
  });
});
