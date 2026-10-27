import { describe, expect, it } from "vitest";

import { HENDERSON_QUESTION, HENDERSON_SHORT_QUESTION, HENDERSON_WHY, THREAD, requestsWorld, routedToBoth } from "../fixtures/requests";
import { members, orgs } from "../fixtures/shell";
import { approvalCaption, cardTitle, eyebrow, lookedAtSummary, routingOf, whyHeading } from "./decision";
import type { ConsultView, EscalationView } from "./types";

const NOW = Date.parse("2026-10-26T10:30:00Z");

function item(threadId: string, world = requestsWorld(NOW)): { escalation: EscalationView; consult: ConsultView | null } {
  const workspace = world.threads.get(threadId)?.workspace;
  const escalation = workspace?.escalations[0];
  if (workspace === undefined || escalation === undefined) throw new Error(`no item on ${threadId}`);
  return { escalation, consult: workspace.consults.find((asked) => asked.id === escalation.consult_id) ?? null };
}

const raj = { id: members.raj.id, slug: "raj", name: members.raj.name };
const henderson = orgs.henderson;
const vantage = orgs.vantage;

describe("eyebrow", () => {
  it("says whose answer Claude needs, naming the assignee for anyone else", () => {
    const { escalation } = item(THREAD.henderson);
    expect(eyebrow(escalation, henderson, members.helen.id)).toBe(
      "Claude needs your answer before replying to Henderson Lab",
    );
    expect(eyebrow(escalation, henderson, members.raj.id)).toBe(
      "Claude needs Helen's answer before replying to Henderson Lab",
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

describe("routingOf", () => {
  it("gives the viewer their own why, and who else has it", () => {
    const { escalation } = item(THREAD.henderson, routedToBoth(requestsWorld(NOW)));
    expect(routingOf(escalation, members.raj.id, orgs.northlake, members.helen.id)).toEqual({
      line: `Routed to you: ${HENDERSON_WHY.raj}`,
      with: "with Helen",
    });
    expect(routingOf(escalation, members.helen.id, orgs.northlake, members.helen.id)).toEqual({
      line: `Routed to you: ${HENDERSON_WHY.helen}`,
      with: "with Raj",
    });
  });

  it("names the assignee's why for someone else's item", () => {
    const { escalation } = item(THREAD.henderson);
    expect(routingOf(escalation, members.raj.id, orgs.northlake, members.helen.id)).toEqual({
      line: `Routed to Helen: “${HENDERSON_WHY.helen}”`,
      with: null,
    });
  });

  it("heads the routing part with whoever the item came to", () => {
    const { escalation } = item(THREAD.henderson);
    expect(whyHeading(escalation, members.helen.id)).toBe("Why it came to you");
    expect(whyHeading(escalation, members.raj.id)).toBe("Why it came to Helen");
    const both = item(THREAD.henderson, routedToBoth(requestsWorld(NOW))).escalation;
    expect(whyHeading(both, members.raj.id)).toBe("Why it came to you");
  });

  it("reads the route the item opened with, never a redraft's", () => {
    const { escalation } = item(THREAD.henderson);
    const [opening] = escalation.drafts;
    if (opening === undefined) throw new Error("no opening draft");
    const redraft = { ...opening, draft_rev: 2, route: { assignees: [{ slug: "raj", why: "you run the XRD" }] } };
    const redrafted = { ...escalation, drafts: [opening, redraft], current_draft_rev: 2 };
    expect(routingOf(redrafted, members.helen.id, orgs.northlake, members.helen.id).line).toBe(
      `Routed to you: ${HENDERSON_WHY.helen}`,
    );
  });

  it("says only who has it once a teammate moved it", () => {
    const { escalation } = item(THREAD.henderson);
    const moved = { ...escalation, assignees: [raj] };
    expect(routingOf(moved, members.helen.id, orgs.northlake, members.helen.id)).toEqual({
      line: "Assigned to Raj",
      with: null,
    });
    expect(routingOf(moved, members.raj.id, orgs.northlake, members.helen.id).line).toBe("Assigned to you");
  });

  it("says only who has a held message or an unnamed consult once a teammate moved it", () => {
    const held = item(THREAD.vantage).escalation;
    const moved = { ...held, assignees: [raj] };
    expect(routingOf(moved, members.raj.id, orgs.northlake, members.helen.id).line).toBe("Assigned to you");
    expect(routingOf(moved, members.helen.id, orgs.northlake, members.helen.id).line).toBe("Assigned to Raj");
    const { escalation } = item(THREAD.henderson);
    const unnamed = {
      ...escalation,
      assignees: [raj],
      drafts: escalation.drafts.map((draft) => ({ ...draft, route: { assignees: [] } })),
    };
    expect(routingOf(unnamed, members.raj.id, orgs.northlake, members.helen.id).line).toBe("Assigned to you");
    // Until the org's setting has loaded, it claims nothing it can't know.
    expect(routingOf(held, members.helen.id, orgs.northlake, undefined).line).toBe("Assigned to you");
  });

  it("says an item routing named no one for, and a held message, went to the default assignee", () => {
    const held = item(THREAD.vantage).escalation;
    expect(routingOf(held, members.helen.id, orgs.northlake, members.helen.id).line).toBe(
      "Sent to you as Northlake Characterization Core's default assignee.",
    );
    expect(routingOf(held, members.raj.id, orgs.northlake, members.helen.id).line).toBe(
      "Sent to Helen as Northlake Characterization Core's default assignee.",
    );
    // The API drops names that aren't the org's members, as routing did.
    const { escalation } = item(THREAD.henderson);
    const unnamed = { ...escalation, drafts: escalation.drafts.map((draft) => ({ ...draft, route: { assignees: [] } })) };
    expect(routingOf(unnamed, members.helen.id, orgs.northlake, members.helen.id).line).toMatch(/^Sent to you as /);
    // Opened without a draft (the answerer failed): routing had no route to read.
    const undrafted = { ...escalation, drafts: [] };
    expect(routingOf(undrafted, members.helen.id, orgs.northlake, members.helen.id).line).toMatch(/^Sent to you as /);
  });
});

describe("approvalCaption", () => {
  it("says what approving does, and for whom when it isn't the viewer's", () => {
    const { escalation } = item(THREAD.henderson);
    expect(approvalCaption(escalation, henderson, members.helen.id, false)).toBe(
      "Approving sends this reply to Henderson Lab only · ⌘↵ to approve",
    );
    expect(approvalCaption(escalation, henderson, members.raj.id, false)).toBe(
      "Approving answers for Helen · sends this reply to Henderson Lab only",
    );
  });

  it("drops the ⌘↵ hint while text is typed, since ⌘↵ then acts on the text", () => {
    const { escalation } = item(THREAD.henderson);
    expect(approvalCaption(escalation, henderson, members.helen.id, true)).toBe(
      "Approving sends this reply to Henderson Lab only",
    );
  });
});

describe("lookedAtSummary", () => {
  it("lists what Claude read by title, and what it searched for", () => {
    const [rev] = item(THREAD.henderson).escalation.drafts;
    if (rev === undefined) throw new Error("no draft");
    expect(lookedAtSummary(rev)).toBe(
      "XRD availability before Nov 6 · Public calendar · Raj's note, reserve XRD block · External academic price list",
    );
    const untitled = { ...rev, activity: [{ tool: "read_document", label: "Read a document", detail: null }] };
    expect(lookedAtSummary(untitled)).toBe("Read a document");
  });
});
