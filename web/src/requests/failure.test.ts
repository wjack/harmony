import { describe, expect, it } from "vitest";

import type { SteerView } from "../api/types";
import { failureLine } from "../chat/failureCopy";
import { members } from "../fixtures/shell";
import { NoResponse } from "./api";
import { blockLine, editError, reopenError, reopenFailure, steerOutcome } from "./failure";
import type { ApiProblem } from "./types";

const helen = { id: members.helen.id, slug: "helen", name: members.helen.name };
const raj = { id: members.raj.id, slug: "raj", name: members.raj.name };

function steer(fields: Partial<SteerView>): SteerView {
  return {
    id: "s1",
    kind: "edit",
    author: helen,
    body: "Keep it shorter.",
    created_at: "2026-10-27T10:00:00Z",
    status: "done",
    escalation_id: null,
    draft_rev: null,
    guidance_outcome: null,
    guidance_version: null,
    draft_outcome: null,
    failure_reason: null,
    version: 2,
    ...fields,
  };
}

const problem = (fields: Partial<ApiProblem> & Pick<ApiProblem, "code">): ApiProblem => ({ detail: "", ...fields });

describe("steerOutcome", () => {
  it("says what Claude did with each half, linking the guidance it now follows", () => {
    expect(steerOutcome(steer({ status: "working" }), "ok", false)).toEqual({
      text: "Claude is working on it…",
      tone: "working",
      action: null,
    });
    expect(steerOutcome(steer({ guidance_outcome: "updated", draft_outcome: "rewritten" }), "ok", true)).toEqual({
      text: "Claude rewrote the draft and will follow this for the rest of the conversation.",
      tone: "done",
      action: "see_guidance",
    });
    expect(steerOutcome(steer({ guidance_outcome: "updated" }), "ok", true).text).toBe(
      "Claude will follow this from its next message in this conversation.",
    );
    expect(steerOutcome(steer({ guidance_outcome: "unchanged", draft_outcome: "rewritten" }), "ok", false)).toEqual({
      text: "Claude rewrote the draft.",
      tone: "done",
      action: null,
    });
    expect(steerOutcome(steer({ guidance_outcome: "unchanged" }), "ok", true).text).toBe("Claude kept its guidance as it was.");
  });

  it("says which half failed, and shows Try again", () => {
    const both = steerOutcome(steer({ status: "failed", guidance_outcome: "failed", failure_reason: "model_error" }), "ok", false);
    expect(both).toEqual({
      text: "Couldn't reach Claude. Your instruction is saved; nothing changed.",
      tone: "failed",
      action: "try_again",
    });
    const draftOnly = steer({ status: "failed", guidance_outcome: "updated", draft_outcome: "failed", failure_reason: "model_error" });
    expect(steerOutcome(draftOnly, "ok", true).text).toBe(
      "Claude will follow this from its next message, but couldn't rewrite the draft. Change it yourself, or try again.",
    );
    const guidanceOnly = steer({ status: "failed", guidance_outcome: "failed", draft_outcome: "rewritten", failure_reason: "invalid" });
    expect(steerOutcome(guidanceOnly, "ok", false).text).toBe(
      "Claude rewrote the draft, but couldn't update what it follows for the rest of the conversation.",
    );
  });

  it("says guidance that added details wasn't used, without naming them", () => {
    expect(steerOutcome(steer({ status: "failed", failure_reason: "added_details" }), "ok", false)).toEqual({
      text: "Claude's guidance added details you didn't give, so it wasn't used. Nothing changed. Try rewording it.",
      tone: "failed",
      action: "try_again",
    });
    const rewrote = steer({ status: "failed", guidance_outcome: "rejected", draft_outcome: "rewritten", failure_reason: "added_details" });
    expect(steerOutcome(rewrote, "ok", false).text).toBe(
      "Claude rewrote the draft, but its guidance for the rest of the conversation added details you didn't give, so it wasn't kept.",
    );
  });

  it("names the spent budget instead of a retry that would fail", () => {
    const spent = steer({ status: "failed", failure_reason: "demo_budget" });
    expect(steerOutcome(spent, "demo_exhausted", false)).toEqual({
      text: `Claude couldn't act on this. ${failureLine("demo_budget", "demo_exhausted")}`,
      tone: "failed",
      action: null,
    });
    expect(steerOutcome(spent, "ok", false).action).toBe("try_again");
  });

  it("says a conversation that closed mid-edit kept the instruction", () => {
    expect(steerOutcome(steer({ status: "failed", failure_reason: "thread_closed" }), "ok", false)).toEqual({
      text: "The conversation closed before Claude finished. Your instruction is saved.",
      tone: "failed",
      action: null,
    });
  });
});

describe("reopenFailure", () => {
  it("gives REOPEN.md §9's line for each reason", () => {
    expect(reopenFailure("unchanged", "ok")).toEqual({
      text: "Claude didn't find anything new to pick up in what you wrote. Try saying what it should do next.",
      retry: true,
    });
    expect(reopenFailure("added_details", "ok").text).toBe(
      "Claude's guidance added details you didn't give, so it wasn't used. Try rewording it.",
    );
    expect(reopenFailure("refused", "ok").text).toBe("Claude declined to write guidance from this.");
    expect(reopenFailure("reopen_failed", "ok").text).toBe("Something went wrong reopening the conversation.");
  });

  it("names the spent budget, with Try again only once it's back", () => {
    expect(reopenFailure("demo_budget", "demo_exhausted")).toEqual({
      text: "Claude couldn't pick this up: the demo's live-model budget is used up.",
      retry: false,
    });
    expect(reopenFailure("demo_budget", "ok").retry).toBe(true);
  });
});

describe("blockLine", () => {
  it("says why Reopen isn't available, and nothing for seeded history", () => {
    const us = "Northlake";
    const them = "Henderson Frame Lab";
    expect(blockLine("history", us, them)).toBeNull();
    expect(blockLine("refused", us, them)).toBe("Henderson Frame Lab's Claude declined to continue, so only Henderson Frame Lab can reopen it.");
    expect(blockLine("limit", us, them)).toBe("This conversation was reopened twice, so it can't be reopened again.");
    expect(blockLine("they_closed", us, them)).toBe(
      "Henderson Frame Lab closed it after Northlake reopened it, so only Henderson Frame Lab can reopen it.",
    );
  });
});

describe("editError", () => {
  it("keeps the key only when nothing answered", () => {
    expect(editError(new NoResponse(), null, helen.id)).toEqual({
      text: "Couldn't reach Harmony. Try again.",
      retry: false,
      sameKey: true,
    });
  });

  it("names whose edit is still running", () => {
    expect(editError(new Error(), problem({ code: "steer_running", by: raj }), helen.id).text).toBe(
      "Claude is still working on Raj's edit. Try again once it lands.",
    );
    expect(editError(new Error(), problem({ code: "steer_running", by: helen }), helen.id).text).toBe(
      "Claude is still working on your edit. Try again once it lands.",
    );
  });

  it("says a closed conversation and a switched member each kept the text", () => {
    expect(editError(new Error(), problem({ code: "thread_closed" }), helen.id).text).toBe(
      "This conversation is closed. Reopen it to continue.",
    );
    expect(editError(new Error(), problem({ code: "member_changed", member: raj }), helen.id).text).toBe(
      "You switched to Raj in another tab, so this wasn't sent.",
    );
  });
});

describe("reopenError", () => {
  const names = { us: "Northlake Characterization Core", them: "Henderson Frame Lab" };

  it("moves the text to Edit when the conversation is open already", () => {
    expect(reopenError(new Error(), problem({ code: "thread_open", reopened_by: { org: { id: "o", slug: "h", name: "Henderson Frame Lab" }, member: null } }), helen.id, names)).toEqual({
      text: "Henderson Frame Lab reopened it first. Your text is in Edit, ready to send.",
      toEdit: true,
    });
    const ours = { org: { id: "n", slug: "n", name: names.us }, member: helen };
    expect(reopenError(new Error(), problem({ code: "thread_open", reopened_by: ours }), helen.id, names).text).toBe(
      "You already reopened this conversation in another tab.",
    );
  });

  it("keeps the prompt for everything else", () => {
    expect(reopenError(new NoResponse(), null, helen.id, names)).toEqual({ text: "Couldn't reach Harmony. Try again.", toEdit: false });
    expect(reopenError(new Error(), problem({ code: "member_changed", member: raj }), helen.id, names).toEdit).toBe(false);
    expect(reopenError(new Error(), problem({ code: "not_reopenable", block: "limit" }), helen.id, names).text).toBe(
      "This conversation was reopened twice, so it can't be reopened again.",
    );
    expect(reopenError(new Error(), problem({ code: "closing_in_progress" }), helen.id, names).text).toBe(
      "Claude is still wrapping up this conversation. Try again in a moment.",
    );
  });
});
