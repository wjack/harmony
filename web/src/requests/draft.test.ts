import { describe, expect, it } from "vitest";

import { hendersonDraft } from "../fixtures/requests";
import { approveBody, declineBody, isEdited, shownAnswer, sortedDrafts } from "./draft";
import type { EscalationDraft } from "./types";

const rev: EscalationDraft = { ...hendersonDraft("2026-10-26T10:20:00Z"), draft_rev: 2 };

describe("approveBody", () => {
  it("sends the answer exactly as shown, with the rev it came from", () => {
    expect(approveBody("consult", rev, {})).toEqual({
      ok: true,
      body: { action: "approve", text: rev.answer, draft_rev: 2 },
    });
  });

  it("sends the member's edit, trimmed", () => {
    const edits = { answer: "  Yes, Wednesday.  " };
    expect(isEdited(edits)).toBe(true);
    expect(approveBody("consult", rev, edits)).toEqual({
      ok: true,
      body: { action: "approve", text: "Yes, Wednesday.", draft_rev: 2 },
    });
  });

  it("won't approve an empty answer, in words that fit the item", () => {
    expect(approveBody("consult", rev, { answer: " " })).toEqual({ ok: false, error: "Write an answer before approving." });
    expect(approveBody("outbound_review", null, {})).toEqual({ ok: false, error: "Write the reply before sending it." });
  });
});

describe("declineBody", () => {
  it("passes the reason on when there is one", () => {
    expect(declineBody(" Calendar's full. ", rev)).toEqual({ action: "decline", text: "Calendar's full.", draft_rev: 2 });
    expect(declineBody("", rev)).toEqual({ action: "decline", text: null, draft_rev: 2 });
  });
});

describe("drafts", () => {
  it("shows the edit over the draft, and pages oldest first", () => {
    expect(shownAnswer(rev, {})).toBe(rev.answer);
    expect(shownAnswer(rev, { answer: "Edited." })).toBe("Edited.");
    expect(shownAnswer(null, {})).toBe("");
    const first = { ...rev, draft_rev: 1 };
    expect(sortedDrafts([rev, first]).map((draft) => draft.draft_rev)).toEqual([1, 2]);
  });
});
