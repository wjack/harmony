import { describe, expect, it } from "vitest";

import { ESCALATION, THREAD, requestsWorld } from "../fixtures/requests";
import { members, orgs } from "../fixtures/shell";
import { firstName } from "../shell/names";
import { buildActivity } from "./activity";
import { buildTimeline, currentDraft, openEscalation, partiesOf, routedSentence } from "./timeline";
import type { EscalationView, ThreadDetail, ThreadWorkspace } from "./types";

const NOW = Date.parse("2026-10-26T10:30:00Z");

function thread(threadId: string): { detail: ThreadDetail; workspace: ThreadWorkspace } {
  const record = requestsWorld(NOW).threads.get(threadId);
  if (record === undefined) throw new Error(`no fixture thread ${threadId}`);
  return { detail: { ...record.detail, access_reason: "routed", access_reasons: ["routed"] }, workspace: record.workspace };
}

const helen = { id: members.helen.id, slug: "helen", name: members.helen.name };
const raj = { id: members.raj.id, slug: "raj", name: members.raj.name };

describe("partiesOf", () => {
  it("puts our org first whichever side we're on", () => {
    const { detail } = thread(THREAD.henderson);
    expect(partiesOf(detail)).toEqual({ us: orgs.northlake, them: orgs.henderson });
    expect(partiesOf({ ...detail, side: "requester" })).toEqual({ us: orgs.henderson, them: orgs.northlake });
  });
});

describe("buildTimeline", () => {
  it("merges the transcript with our side's rows in time order", () => {
    const { detail, workspace } = thread(THREAD.henderson);
    const entries = buildTimeline(detail, workspace);
    expect(entries.map((entry) => entry.type)).toEqual(["message", "consult", "note", "note"]);
    const [request] = entries;
    expect(request?.type === "message" && request.ours).toBe(false);
  });

  it("shows only the transcript until the workspace loads", () => {
    const { detail } = thread(THREAD.henderson);
    expect(buildTimeline(detail, undefined).every((entry) => entry.type === "message")).toBe(true);
  });

  it("names who released an answer, and draws a close as its own row", () => {
    const { detail, workspace } = thread(THREAD.ridgeline);
    const entries = buildTimeline(detail, workspace);
    const release = entries.find((entry) => entry.type === "release");
    expect(release?.type === "release" && release.by?.slug).toBe("helen");
    const sent = entries.find((entry) => entry.type === "message" && entry.ours);
    expect(sent?.type === "message" && sent.approvedBy?.slug).toBe("helen");
    // A consult's approval shows as its release, not again as a resolution.
    expect(entries.some((entry) => entry.type === "resolution")).toBe(false);
    const last = entries.at(-1);
    expect(last?.type === "close" && [last.ours, last.org.slug]).toEqual([false, orgs.ridgeline.slug]);
  });

  it("shows an approved held message as a resolution beside the message it sent", () => {
    const { detail, workspace } = thread(THREAD.survey);
    const entries = buildTimeline(detail, workspace);
    expect(entries.filter((entry) => entry.type === "resolution")).toHaveLength(1);
    const sent = entries.find((entry) => entry.type === "message" && entry.ours);
    expect(sent?.type === "message" && [sent.provenance, sent.approvedBy?.slug]).toEqual(["agent_approved", "helen"]);
  });

  it("shows an automatic release, with no teammate named", () => {
    const { detail, workspace } = thread(THREAD.pacific);
    const release = buildTimeline(detail, workspace).find((entry) => entry.type === "release");
    expect(release?.type === "release" && release.by).toBeNull();
  });

  it("orders a question before its release and the message it produced at the same instant", () => {
    const { detail, workspace } = thread(THREAD.pacific);
    const answer = detail.messages.find((message) => message.seq === 2);
    const at = answer?.created_at ?? "";
    const sameInstant: ThreadWorkspace = {
      ...workspace,
      consults: workspace.consults.map((consult) => ({ ...consult, created_at: at, released_at: at })),
    };
    const ids = buildTimeline(detail, sameInstant).map((entry) => entry.id);
    const consultId = workspace.consults[0]?.id ?? "";
    expect(ids.indexOf(`consult:${consultId}`)).toBeLessThan(ids.indexOf(`release:${consultId}`));
    expect(ids.indexOf(`release:${consultId}`)).toBeLessThan(ids.indexOf(answer?.id ?? ""));
  });
});

describe("openEscalation", () => {
  const [base] = thread(THREAD.henderson).workspace.escalations;
  if (base === undefined) throw new Error("no Henderson escalation");
  const later: EscalationView = { ...base, id: "later", assignees: [raj], created_at: "2026-10-26T10:29:00Z" };

  it("prefers the acting member's own item, then the oldest", () => {
    expect(openEscalation([base, later], members.raj.id)?.id).toBe("later");
    expect(openEscalation([later, base], members.helen.id)?.id).toBe(ESCALATION.henderson);
    expect(openEscalation([later, base], undefined)?.id).toBe(ESCALATION.henderson);
  });

  it("ignores resolved items", () => {
    expect(openEscalation([{ ...base, status: "resolved" }], members.helen.id)).toBeNull();
  });
});

describe("routedSentence", () => {
  const [base] = thread(THREAD.henderson).workspace.escalations;
  if (base === undefined) throw new Error("no Henderson escalation");
  const routedTo = (assignees: string[]): EscalationView => ({
    ...base,
    drafts: base.drafts.map((draft) => ({ ...draft, route: { assignees, reason: "Helen owns the schedule." } })),
  });

  it("gives Claude's reason while the item is with the assignees it proposed", () => {
    expect(currentDraft(base)?.draft_rev).toBe(1);
    expect(routedSentence(base, members.helen.id, firstName)).toBe(
      `Routed to you by Claude: ${currentDraft(base)?.route?.reason ?? ""}`,
    );
    expect(routedSentence(base, members.raj.id, firstName)).toMatch(/^Routed to Helen by Claude: /);
    expect(routedSentence(routedTo(["helen", "raj"]), members.helen.id, firstName)).toMatch(/^Routed to you by Claude/);
  });

  it("says a question whose draft named no one on the team went to the default assignee", () => {
    // The API drops names that aren't the org's members, as routing did.
    const fellBack = "Claude named no one on the team, so it first went to the default assignee";
    expect(routedSentence(routedTo([]), members.helen.id, firstName)).toBe(`Assigned to you. ${fellBack}`);
    // Still true once a teammate moved it: nothing records the move.
    expect(routedSentence({ ...routedTo([]), assignees: [raj] }, members.helen.id, firstName)).toBe(
      `Assigned to Raj. ${fellBack}`,
    );
    // Opened without a draft (the answerer failed): routing had no route to read.
    expect(routedSentence({ ...base, drafts: [] }, members.helen.id, firstName)).toBe(`Assigned to you. ${fellBack}`);
  });

  it("reads the route the item opened with, never a redraft's", () => {
    const [opening] = base.drafts;
    if (opening === undefined) throw new Error("no opening draft");
    const redraft = { ...opening, draft_rev: 2, route: { assignees: ["raj"], reason: "Raj runs the XRD." } };
    const redrafted: EscalationView = { ...base, drafts: [opening, redraft], current_draft_rev: 2 };
    expect(routedSentence(redrafted, members.helen.id, firstName)).toBe(
      `Routed to you by Claude: ${opening.route?.reason ?? ""}`,
    );
    expect(routedSentence({ ...redrafted, assignees: [raj] }, members.helen.id, firstName)).toBe("Assigned to Raj");
  });

  it("says only who has it once a teammate moved it, or for a held message", () => {
    expect(routedSentence({ ...base, assignees: [raj] }, members.helen.id, firstName)).toBe("Assigned to Raj");
    const [held] = thread(THREAD.vantage).workspace.escalations;
    expect(held && routedSentence(held, members.raj.id, firstName)).toBe("Assigned to Helen");
    expect(routedSentence({ ...base, assignees: [helen] }, members.helen.id, firstName)).toMatch(/^Routed to you/);
  });
});

describe("buildActivity", () => {
  it("logs consults, the flag and notes oldest first", () => {
    const { workspace } = thread(THREAD.henderson);
    const texts = buildActivity(workspace, orgs.northlake).map((entry) => entry.text);
    expect(texts[0]).toMatch(/asked the team a question/);
    expect(texts).toContain("Claude asked a teammate to check the answer");
    expect(texts.filter((text) => text === "added an internal note")).toHaveLength(2);
  });

  it("logs an automatic answer and a teammate's approval", () => {
    expect(buildActivity(thread(THREAD.pacific).workspace, orgs.northlake).map((entry) => entry.text)).toContain(
      "Claude answered without a teammate",
    );
    const survey = buildActivity(thread(THREAD.survey).workspace, orgs.northlake);
    expect(survey.map((entry) => [entry.actor?.slug ?? null, entry.text])).toContainEqual(["helen", "approved the reply"]);
  });
});
