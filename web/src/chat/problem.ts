import { ApiError } from "../api/client";
import type { ApiProblem } from "./types";

/** The route's typed `ApiProblem` (code and resolver), when the failure carried one. */
export function problemOf(error: unknown): ApiProblem | null {
  return error instanceof ApiError && error.problem !== null ? (error.problem as ApiProblem) : null;
}

/** What to tell the member when an action failed, in the product's voice. */
export function describeFailure(error: unknown): string {
  const problem = problemOf(error);
  if (problem === null) {
    if (error instanceof ApiError && error.invalid.includes("string_too_long")) {
      return "That's too long to send. Shorten it and try again.";
    }
    return error instanceof ApiError && error.status >= 500
      ? "Something went wrong. Try again in a moment."
      : "That didn't go through. Try again.";
  }
  switch (problem.code) {
    case "card_not_pending":
      return "This was already answered.";
    case "escalation_not_open":
      return problem.resolved_by ? `Already answered by ${problem.resolved_by.name}.` : "This was already answered.";
    case "world_retired":
      return "This demo world was reset. Reload to continue.";
    default:
      return problem.detail;
  }
}
