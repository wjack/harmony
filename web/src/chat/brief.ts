/** The outreach card's brief and asks: free text Claude wrote, which the member may edit before sending. */
import { plural } from "./format";
import type { ScreenedCounts } from "./types";

/** The longest brief, or ask, the API takes (`agents.models.BRIEF_MAX_CHARS`). */
export const BRIEF_MAX_CHARS = 8000;

/** The member's version of a brief or an ask to send, or null when it reads the same as Claude's. */
export function editedText(original: string, draft: string): string | null {
  const text = draft.trim();
  return text === "" || text === original.trim() ? null : text;
}

/**
 * The brief as one run of plain text, for a clamped preview: no line breaks, list
 * markers or Markdown marks. Each line becomes a sentence, so the title doesn't run
 * into the body.
 */
export function briefGist(text: string): string {
  return text
    .split("\n")
    .map((line) =>
      line
        .replace(/^\s*(?:[-*+]|\d+[.)])\s+/, "")
        .replace(/[*_`#>]+/g, "")
        .replace(/\s+/g, " ")
        .trim(),
    )
    .filter((line) => line !== "")
    .map((line) => (/[.:;!?]$/.test(line) ? line : `${line}.`))
    .join(" ");
}

/** "Screened 34 organizations: 3 strong, 4 possible". */
export function screenedLine(screened: ScreenedCounts): string {
  const fits = [
    screened.strong > 0 ? `${screened.strong} strong` : null,
    screened.possible > 0 ? `${screened.possible} possible` : null,
  ].filter((part) => part !== null);
  const head = `Screened ${plural(screened.total, "organization")}`;
  return fits.length === 0 ? head : `${head}: ${fits.join(", ")}`;
}
