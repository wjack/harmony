import { describe, expect, it } from "vitest";

import {
  EDIT_INSTRUCTION,
  ESCALATION,
  REOPEN_INSTRUCTION,
  THREAD,
  edited,
  reopenFailed,
  reopenWorking,
  reopened,
  requestsWorld,
  rewritten,
} from "../fixtures/requests";
import { members, orgs } from "../fixtures/shell";
import { buildActivity } from "./activity";
import { buildTimeline, openEscalation, partiesOf, type SteerEntry } from "./timeline";
import type { EscalationView, ThreadDetail, ThreadWorkspace } from "./types";

const NOW = Date.parse("2026-10-26T10:30:00Z");

function thread(threadId: string, world = requestsWorld(NOW)): { detail: ThreadDetail; workspace: ThreadWorkspace } {
  const record = world.threads.get(threadId);
  if (record === undefined) throw new Error(`no fixture thread ${threadId}`);
  return { detail: { ...record.detail, access_reason: "routed", access_reasons: ["routed"] }, workspace: record.workspace };
}

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
    // Our Claude asking the team is no row of its own: the card shows the question.
    expect(entries.map((entry) => entry.type)).toEqual(["message", "note", "note"]);
    const [request] = entries;
    expect(request?.type === "message" && request.ours).toBe(false);
  });

  it("shows only the transcript until the workspace loads", () => {
    const { detail } = thread(THREAD.henderson);
    expect(buildTimeline(detail, undefined).every((entry) => entry.type === "message")).toBe(true);
  });

  it("names who released an answer, and ends a closed run with its Closed row and our summary", () => {
    const { detail, workspace } = thread(THREAD.ridgeline);
    const entries = buildTimeline(detail, workspace);
    const release = entries.find((entry) => entry.type === "release");
    expect(release?.type === "release" && release.by?.slug).toBe("helen");
    const sent = entries.find((entry) => entry.type === "message" && entry.ours);
    expect(sent?.type === "message" && sent.approvedBy?.slug).toBe("helen");
    // A consult's approval shows as its release, not again as a resolution.
    expect(entries.some((entry) => entry.type === "resolution")).toBe(false);
    expect(entries.slice(-2).map((entry) => entry.type)).toEqual(["close", "ended"]);
    const ended = entries.at(-1);
    expect(ended?.type === "ended" && [ended.ending, ended.latest, ended.summary]).toEqual([
      { endReason: "closed", closedBy: "requester", stopReason: null },
      true,
      workspace.close_summary,
    ]);
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

  it("orders a release before the message it produced at the same instant", () => {
    const { detail, workspace } = thread(THREAD.pacific);
    const answer = detail.messages.find((message) => message.seq === 2);
    const at = answer?.created_at ?? "";
    const sameInstant: ThreadWorkspace = {
      ...workspace,
      consults: workspace.consults.map((consult) => ({ ...consult, created_at: at, released_at: at })),
    };
    const ids = buildTimeline(detail, sameInstant).map((entry) => entry.id);
    const consultId = workspace.consults[0]?.id ?? "";
    expect(ids.indexOf(`release:${consultId}`)).toBeLessThan(ids.indexOf(answer?.id ?? ""));
  });

  it("orders a close, the run's end and its reopening at the same instant", () => {
    const record = thread(THREAD.ridgeline, reopened(requestsWorld(NOW), THREAD.ridgeline));
    const close = record.detail.messages.find((message) => message.kind === "close");
    const at = close?.created_at ?? "";
    const detail: ThreadDetail = {
      ...record.detail,
      runs: record.detail.runs.map((run) => ({
        ...run,
        started_at: run.run === 1 ? run.started_at : at,
        ended: run.ended === null ? null : { ...run.ended, at },
      })),
    };
    const types = buildTimeline(detail, record.workspace)
      .filter((entry) => entry.at === at)
      .map((entry) => entry.type);
    expect(types).toEqual(["close", "ended", "reopen"]);
  });

  it("shows each of our Edits as its own row", () => {
    const record = thread(THREAD.pacific, edited(requestsWorld(NOW), THREAD.pacific));
    const steers = buildTimeline(record.detail, record.workspace).filter((entry): entry is SteerEntry => entry.type === "steer");
    expect(steers.map((entry) => entry.steer.body)).toEqual([EDIT_INSTRUCTION]);
  });

  it("folds every reopen steer into a reopen row, never an Edit's", () => {
    for (const make of [reopenWorking, reopenFailed]) {
      const record = thread(THREAD.ridgeline, make(requestsWorld(NOW), THREAD.ridgeline));
      const entries = buildTimeline(record.detail, record.workspace);
      expect(entries.some((entry) => entry.type === "steer")).toBe(false);
      const row = entries.at(-1);
      expect(row?.type === "reopen" && [row.ours, row.by?.slug, row.steer?.body, row.outcome]).toEqual([
        true,
        "helen",
        REOPEN_INSTRUCTION,
        make === reopenWorking ? "working" : "failed",
      ]);
    }
  });

  it("keeps the first run's Closed row and summary after our reopen, and joins the reopen to what Helen typed", () => {
    const summary = thread(THREAD.ridgeline).workspace.close_summary;
    const record = thread(THREAD.ridgeline, reopened(requestsWorld(NOW), THREAD.ridgeline));
    const entries = buildTimeline(record.detail, record.workspace);
    const ended = entries.find((entry) => entry.type === "ended");
    expect(ended?.type === "ended" && [ended.run, ended.latest, ended.summary]).toEqual([1, false, summary]);
    const reopen = entries.find((entry) => entry.type === "reopen");
    expect(reopen?.type === "reopen" && [reopen.ours, reopen.outcome, reopen.steer?.body]).toEqual([
      true,
      "opened",
      REOPEN_INSTRUCTION,
    ]);
    expect(entries.at(-1)?.type).toBe("message");
  });

  it("names only the org when the counterparty reopened it", () => {
    const record = thread(THREAD.ridgeline, reopened(requestsWorld(NOW), THREAD.ridgeline, "them"));
    const reopen = buildTimeline(record.detail, record.workspace).find((entry) => entry.type === "reopen");
    expect(reopen?.type === "reopen" && [reopen.ours, reopen.org.slug, reopen.by, reopen.steer]).toEqual([
      false,
      orgs.ridgeline.slug,
      null,
      null,
    ]);
  });

  it("reads a run's end from the thread itself when the server lists no runs", () => {
    const { detail, workspace } = thread(THREAD.pacific);
    const stoppedThread: ThreadDetail = {
      ...detail,
      state: "closed",
      stop_reason: "demo_budget",
      end_reason: null,
      ended_at: null,
      runs: [],
    };
    const ended = buildTimeline(stoppedThread, workspace).at(-1);
    expect(ended?.type === "ended" && [ended.ending, ended.at, ended.latest]).toEqual([
      { endReason: "stopped", closedBy: null, stopReason: "demo_budget" },
      detail.last_activity_at,
      true,
    ]);
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

describe("buildActivity", () => {
  it("logs the flag and comments oldest first", () => {
    const { workspace } = thread(THREAD.henderson);
    const texts = buildActivity(workspace).map((entry) => entry.text);
    expect(texts).toContain("Claude asked a teammate to check the answer");
    expect(texts.filter((text) => text === "added a comment")).toHaveLength(2);
  });

  it("logs an automatic answer and a teammate's approval", () => {
    expect(buildActivity(thread(THREAD.pacific).workspace).map((entry) => entry.text)).toContain(
      "Claude answered without a teammate",
    );
    const survey = buildActivity(thread(THREAD.survey).workspace);
    expect(survey.map((entry) => [entry.actor?.slug ?? null, entry.text])).toContainEqual(["helen", "approved the reply"]);
  });

  it("logs an Edit and the draft Claude rewrote from it", () => {
    const record = thread(THREAD.henderson, rewritten(requestsWorld(NOW), THREAD.henderson));
    const texts = buildActivity(record.workspace).map((entry) => [entry.actor?.slug ?? null, entry.text]);
    expect(texts).toContainEqual(["helen", "told Claude what to do differently"]);
    expect(texts).toContainEqual([null, "Claude rewrote the draft from Helen's edit (rev 2)"]);
  });

  it("logs a reopen once it went through, and never a failed attempt", () => {
    const opened = thread(THREAD.ridgeline, reopened(requestsWorld(NOW), THREAD.ridgeline));
    const failed = thread(THREAD.ridgeline, reopenFailed(requestsWorld(NOW), THREAD.ridgeline));
    expect(buildActivity(opened.workspace).map((entry) => entry.text)).toContain("reopened the conversation");
    expect(buildActivity(failed.workspace).map((entry) => entry.text)).not.toContain("reopened the conversation");
  });
});
