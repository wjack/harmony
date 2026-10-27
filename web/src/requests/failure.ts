/**
 * What the request view says about an Edit or a reopen after the fact (EDIT.md §8.3,
 * REOPEN.md §3.2 and §9): each steer's outcome line, a failed reopen's reason, why
 * Reopen isn't available, and why a submit didn't go through. A spent live-model budget
 * fails every retry, so while one is spent the copy says so and shows no Try again.
 */
import type { ReopenBlock, SteerView } from "../api/types";
import type { BudgetState } from "../app/budget";
import { budgetLine, failureLine, stoppedLine, type FailureReason } from "../chat/failureCopy";
import { firstName } from "../shell/names";
import { NoResponse } from "./api";
import { possessive } from "./format";
import type { ApiProblem } from "./types";

export type SteerFailure = NonNullable<SteerView["failure_reason"]>;

type BudgetReason = Extract<FailureReason, "session_budget" | "daily_budget" | "demo_budget" | "paused">;

function isBudget(reason: SteerFailure | null): reason is BudgetReason {
  return reason === "session_budget" || reason === "daily_budget" || reason === "demo_budget" || reason === "paused";
}

/** "the daily live-model budget ran out", to follow a colon. */
function clause(sentence: string): string {
  return sentence.charAt(0).toLowerCase() + sentence.slice(1).replace(/\.$/, "");
}

// --- An Edit's outcome -----------------------------------------------------------------

export interface OutcomeLine {
  text: string;
  /** `working` spins; `failed` reads in the warning color. */
  tone: "working" | "done" | "failed";
  /** The link after the line: open Details at the guidance, or put the text back in Edit. */
  action: "see_guidance" | "try_again" | null;
}

/** The outcome line under one of our Edits, by how its two halves went (EDIT.md §8.3). */
export function steerOutcome(steer: SteerView, budget: BudgetState, hasGuidance: boolean): OutcomeLine {
  const updated = steer.guidance_outcome === "updated";
  const rewrote = steer.draft_outcome === "rewritten";
  if (steer.status === "working") return { text: "Claude is working on it…", tone: "working", action: null };
  if (steer.status === "done") {
    if (rewrote && updated) {
      return {
        text: "Claude rewrote the draft and will follow this for the rest of the conversation.",
        tone: "done",
        action: "see_guidance",
      };
    }
    if (updated) {
      return { text: "Claude will follow this from its next message in this conversation.", tone: "done", action: "see_guidance" };
    }
    if (rewrote) return { text: "Claude rewrote the draft.", tone: "done", action: hasGuidance ? "see_guidance" : null };
    return { text: "Claude kept its guidance as it was.", tone: "done", action: null };
  }
  const reason = steer.failure_reason;
  if (reason === "thread_closed") {
    return { text: "The conversation closed before Claude finished. Your instruction is saved.", tone: "failed", action: null };
  }
  if (isBudget(reason)) {
    // The line names the cap that's spent now; once nothing is, trying again may work.
    return {
      text: `Claude couldn't act on this. ${failureLine(reason, budget)}`,
      tone: "failed",
      action: budget === "ok" ? "try_again" : null,
    };
  }
  if (reason === "added_details") {
    return {
      text: "Claude's guidance added details you didn't give, so it wasn't used. Nothing changed. Try rewording it.",
      tone: "failed",
      action: "try_again",
    };
  }
  if (rewrote) {
    return {
      text: "Claude rewrote the draft, but couldn't update what it follows for the rest of the conversation.",
      tone: "failed",
      action: "try_again",
    };
  }
  if (updated) {
    return {
      text: "Claude will follow this from its next message, but couldn't rewrite the draft. Change it yourself, or try again.",
      tone: "failed",
      action: "try_again",
    };
  }
  return { text: "Couldn't reach Claude. Your instruction is saved; nothing changed.", tone: "failed", action: "try_again" };
}

// --- Reopen --------------------------------------------------------------------------

/** Why one of our reopen attempts failed (REOPEN.md §9), and whether Try again could work now. */
export function reopenFailure(reason: SteerFailure | null, budget: BudgetState): { text: string; retry: boolean } {
  if (isBudget(reason) && reason !== "paused") {
    const why = budgetLine(budget) ?? stoppedLine(reason, budget);
    return { text: `Claude couldn't pick this up: ${clause(why)}.`, retry: budget === "ok" };
  }
  switch (reason) {
    case "model_error":
    case "paused":
      return { text: `Claude couldn't pick this up: ${clause(stoppedLine(reason, budget))}.`, retry: true };
    case "refused":
      return { text: "Claude declined to write guidance from this.", retry: true };
    case "added_details":
      return { text: "Claude's guidance added details you didn't give, so it wasn't used. Try rewording it.", retry: true };
    case "invalid":
      return { text: "Claude's guidance came back unreadable.", retry: true };
    case "unchanged":
      return {
        text: "Claude didn't find anything new to pick up in what you wrote. Try saying what it should do next.",
        retry: true,
      };
    default:
      return { text: "Something went wrong reopening the conversation.", retry: true };
  }
}

/** Why Reopen isn't available, in place of the button (REOPEN.md §3.2); nothing for seeded history. */
export function blockLine(block: ReopenBlock, us: string, them: string): string | null {
  switch (block) {
    case "history":
      return null;
    case "unfinished":
      return "This conversation can't be reopened: Claude didn't finish wrapping it up.";
    case "limit":
      return "This conversation was reopened twice, so it can't be reopened again.";
    case "refused":
      return `${possessive(them)} Claude declined to continue, so only ${them} can reopen it.`;
    case "they_closed":
      return `${them} closed it after ${us} reopened it, so only ${them} can reopen it.`;
    case "not_asked":
      return `Only ${them} can reopen this conversation.`;
  }
}

// --- Submit errors -----------------------------------------------------------------------

const NO_RESPONSE = "Couldn't reach Harmony. Try again.";
const RETIRED = "This demo world was reset. Reload to continue.";

/** "You switched to Raj in another tab, so this wasn't sent." */
function switched(problem: ApiProblem): string {
  const who = problem.member == null ? "another member" : firstName(problem.member.name);
  return `You switched to ${who} in another tab, so this wasn't sent.`;
}

/** Why an Edit wasn't sent (EDIT.md §8.3); the typed text stays in every case. */
export interface EditError {
  text: string;
  /** Show Try again in the line: only while another Edit is still running. */
  retry: boolean;
  /** Whether a resend is the same write (no answer came back) or a new one. */
  sameKey: boolean;
}

export function editError(error: unknown, problem: ApiProblem | null, viewerId: string | undefined): EditError {
  if (error instanceof NoResponse) return { text: NO_RESPONSE, retry: false, sameKey: true };
  switch (problem?.code) {
    case "steer_running": {
      const by = problem.by;
      const whose = by == null ? "another" : by.id === viewerId ? "your" : possessive(firstName(by.name));
      return { text: `Claude is still working on ${whose} edit. Try again once it lands.`, retry: true, sameKey: false };
    }
    case "thread_closed":
      return { text: "This conversation is closed. Reopen it to continue.", retry: false, sameKey: false };
    case "member_changed":
      return { text: switched(problem), retry: false, sameKey: false };
    case "world_retired":
      return { text: RETIRED, retry: false, sameKey: false };
    default:
      return { text: "Couldn't reach Claude. Try again.", retry: false, sameKey: false };
  }
}

/**
 * Why a reopen wasn't sent (REOPEN.md §9). `toEdit` is set when the conversation is
 * already open again: the prompt closes and its text moves to the composer's Edit.
 */
export interface ReopenError {
  text: string;
  toEdit: boolean;
}

export function reopenError(
  error: unknown,
  problem: ApiProblem | null,
  viewerId: string | undefined,
  { us, them }: { us: string; them: string },
): ReopenError {
  if (error instanceof NoResponse) return { text: NO_RESPONSE, toEdit: false };
  const by = problem?.reopened_by ?? null;
  switch (problem?.code) {
    case "member_changed":
      return { text: switched(problem), toEdit: false };
    case "thread_open": {
      if (by === null) return { text: "This conversation is open again.", toEdit: true };
      if (by.member?.id === viewerId) return { text: "You already reopened this conversation in another tab.", toEdit: true };
      const who = by.member == null ? by.org.name : firstName(by.member.name);
      return { text: `${who} reopened it first. Your text is in Edit, ready to send.`, toEdit: true };
    }
    case "reopen_in_progress":
      return by?.member == null
        ? { text: "Couldn't reopen it just now. Try again in a moment.", toEdit: false }
        : { text: `${firstName(by.member.name)} is reopening this conversation. Try again once it's open.`, toEdit: false };
    case "closing_in_progress":
      return { text: "Claude is still wrapping up this conversation. Try again in a moment.", toEdit: false };
    case "not_reopenable":
      return {
        text: (problem.block == null ? null : blockLine(problem.block, us, them)) ?? "This conversation can't be reopened.",
        toEdit: false,
      };
    case "world_retired":
      return { text: RETIRED, toEdit: false };
    default:
      return { text: "Something went wrong reopening the conversation. Try again.", toEdit: false };
  }
}
