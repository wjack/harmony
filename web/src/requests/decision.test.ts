import { describe, expect, it } from "vitest";

import { HENDERSON_QUESTION, HENDERSON_SHORT_QUESTION, THREAD, requestsWorld } from "../fixtures/requests";
import { members, orgs } from "../fixtures/shell";
import {
  approveHint,
  cardTitle,
  declineCopy,
  draftLabel,
  eyebrow,
  lookedAtSummary,
  modeLine,
  reasonLine,
  sharingLabel,
} from "./decision";
import type { ConsultView, EscalationView } from "./types";

const NOW = Date.parse("2026-10-26T10:30:00Z");

function item(threadId: string, world = requestsWorld(NOW)): { escalation: EscalationView; consult: ConsultView | null } {
  const workspace = world.threads.get(threadId)?.workspace;
  const escalation = workspace?.escalations[0];
  if (workspace === undefined || escalation === undefined) throw new Error(`no item on ${threadId}`);
  return { escalation, consult: workspace.consults.find((asked) => asked.id === escalation.consult_id) ?? null };
}

const henderson = orgs.henderson;
const vantage = orgs.vantage;

describe("eyebrow", () => {
  it("says whose answer Claude needs, naming the assignee for anyone else", () => {
    const { escalation } = item(THREAD.henderson);
    expect(eyebrow(escalation, henderson, members.helen.id)).toBe(
      "Claude needs your answer before replying to Henderson Frame Lab",
    );
    expect(eyebrow(escalation, henderson, members.raj.id)).toBe(
      "Claude needs Helen's answer before replying to Henderson Frame Lab",
    );
  });

  it("says a held message waits to be sent, and who reviews it", () => {
    const { escalation } = item(THREAD.vantage);
    expect(eyebrow(escalation, vantage, members.helen.id)).toBe("Claude wants to send this to Vantage Market Intelligence");
    expect(eyebrow(escalation, vantage, members.raj.id)).toBe(
      "Claude wants to send this to Vantage Market Intelligence · Helen reviews",
    );
  });
});

describe("cardTitle", () => {
  it("is the answerer's short question, else the representative's whole question", () => {
    const { escalation, consult } = item(THREAD.henderson);
    const [rev] = escalation.drafts;
    expect(cardTitle(escalation, rev ?? null, consult, henderson)).toBe(HENDERSON_SHORT_QUESTION);
    // A draft from before short questions, or an item opened without a draft.
    const old = escalation.drafts.map((draft) => ({ ...draft, short_question: null }));
    expect(cardTitle({ ...escalation, drafts: old }, old[0] ?? null, consult, henderson)).toBe(HENDERSON_QUESTION);
    expect(cardTitle({ ...escalation, drafts: [] }, null, consult, henderson)).toBe(HENDERSON_QUESTION);
  });

  it("keeps the short question when the member pages back to an older draft without one", () => {
    const { escalation, consult } = item(THREAD.henderson);
    const [first] = escalation.drafts;
    if (first === undefined) throw new Error("no draft");
    const older = { ...first, short_question: null };
    const newer = { ...first, draft_rev: 2 };
    expect(cardTitle({ ...escalation, drafts: [older, newer] }, older, consult, henderson)).toBe(HENDERSON_SHORT_QUESTION);
  });

  it("asks whether to send a held reply", () => {
    const { escalation, consult } = item(THREAD.vantage);
    expect(cardTitle(escalation, escalation.drafts[0] ?? null, consult, vantage)).toBe(
      "Send this reply to Vantage Market Intelligence?",
    );
  });
});

describe("reasonLine", () => {
  it("says why the item needs a person, and nothing when it's only Ask mode", () => {
    const { escalation } = item(THREAD.henderson);
    expect(reasonLine(escalation)).toMatch(/^The answer says another client released the Nov 4 compression block/);
    // A held message in Ask mode: the card's foot already says which mode.
    expect(reasonLine(item(THREAD.vantage).escalation)).toBeNull();
    expect(reasonLine({ ...escalation, reason: "  " })).toBeNull();
  });
});

describe("the card's words", () => {
  it("names the draft, and what approving it does", () => {
    const consult = item(THREAD.henderson).escalation;
    const held = item(THREAD.vantage).escalation;
    expect(draftLabel(consult)).toBe("Draft reply");
    expect(draftLabel(held)).toBe("Claude's message");
    expect(approveHint(consult, henderson, false)).toBe("Claude replies to Henderson Frame Lab with this answer");
    expect(approveHint(consult, henderson, true)).toBe("Claude replies to Henderson Frame Lab with your edited answer");
    expect(approveHint(held, vantage, true)).toBe("Sends this message to Vantage Market Intelligence as shown");
  });

  it("says what declining does, and who may see the reason", () => {
    expect(declineCopy(item(THREAD.henderson).escalation, henderson)).toEqual({
      title: "Decline to answer",
      caption: "Claude tells Henderson Frame Lab you can't answer, and may pass on your reason.",
    });
    expect(declineCopy(item(THREAD.vantage).escalation, vantage).title).toBe("Decline this message");
  });

  it("names the mode a held message waits in, and links admins to the sharing to edit", () => {
    expect(modeLine("Ask", vantage)).toBe("Ask mode with Vantage Market Intelligence");
    expect(sharingLabel(true, henderson)).toBe("Edit what you share with Henderson Frame Lab");
    expect(sharingLabel(false, henderson)).toBe("See what you share with Henderson Frame Lab");
  });
});

describe("lookedAtSummary", () => {
  it("lists what Claude read by title, and what it searched for", () => {
    const [rev] = item(THREAD.henderson).escalation.drafts;
    if (rev === undefined) throw new Error("no draft");
    expect(lookedAtSummary(rev)).toBe(
      "Compression availability before Nov 6 · Public calendar · Raj's note, Nov 4 compression block · Price list, external academic",
    );
    const untitled = { ...rev, activity: [{ tool: "read_document", label: "Read a document", detail: null }] };
    expect(lookedAtSummary(untitled)).toBe("Read a document");
  });
});
