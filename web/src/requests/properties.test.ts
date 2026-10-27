import { describe, expect, it } from "vitest";

import { THREAD, reopened, requestsWorld, stopped, type RequestsWorld } from "../fixtures/requests";
import { members } from "../fixtures/shell";
import { propertiesOf } from "./properties";
import type { ThreadDetail } from "./types";

const NOW = Date.parse("2026-10-26T10:30:00Z");

function record(threadId: string, world: RequestsWorld = requestsWorld(NOW)) {
  const found = world.threads.get(threadId);
  if (found === undefined) throw new Error(`no thread ${threadId}`);
  const detail: ThreadDetail = { ...found.detail, access_reason: "routed", access_reasons: ["routed"] };
  const open = found.workspace.escalations.find((escalation) => escalation.status === "open") ?? null;
  return { detail, open };
}

describe("propertiesOf", () => {
  it("names who an open item needs, as what it asks when it's yours", () => {
    const { detail, open } = record(THREAD.henderson);
    expect(propertiesOf(detail, open, members.helen.id).status).toEqual({
      label: "Needs your answer",
      tone: "warning",
      needs: true,
    });
    expect(propertiesOf(detail, open, members.raj.id).status).toEqual({
      label: "Waiting on Helen",
      tone: "warning",
      needs: true,
    });
    const held = record(THREAD.vantage);
    expect(propertiesOf(held.detail, held.open, members.helen.id).status.label).toBe("Reply needs your review");
    // The conversation's timeout, not a date anyone asked for: Details says when it closes.
    const properties = propertiesOf(detail, open, members.helen.id);
    expect([properties.closesAt, properties.closedAt, properties.reopened]).toEqual([detail.deadline_at, null, null]);
  });

  it("says who shared a live thread with the viewer, and falls back to routed members", () => {
    const live = record(THREAD.calder);
    const properties = propertiesOf(live.detail, live.open, members.helen.id);
    expect(properties.status).toEqual({ label: "Shared by Raj", tone: "accent", needs: false });
    expect(properties.assignees.map((assignee) => assignee.slug)).toEqual(["raj"]);
    // Raj can see it because it was routed to him: where it stands, not who shared it.
    expect(propertiesOf(live.detail, live.open, members.raj.id).status).toEqual({
      label: "Claude is handling",
      tone: "info",
      needs: false,
    });
  });

  it("doesn't say who shared a thread that was also routed to the viewer", () => {
    const live = record(THREAD.calder);
    const { helen } = members;
    const routed = {
      member_id: helen.id,
      member_slug: helen.slug,
      member_name: helen.name,
      reason: "routed" as const,
      shared_by_member_id: null,
      question: null,
    };
    const detail = { ...live.detail, access: [...live.detail.access, routed] };
    expect(propertiesOf(detail, live.open, helen.id).status).toEqual({
      label: "Claude is handling",
      tone: "info",
      needs: false,
    });
  });

  it("says Closed however a thread ended, with when", () => {
    const ended = record(THREAD.ridgeline);
    const properties = propertiesOf(ended.detail, ended.open, members.helen.id);
    expect(properties.status).toEqual({ label: "Closed", tone: "muted", needs: false });
    // An ended conversation has nothing left to close.
    expect([properties.closesAt, properties.closedAt]).toEqual([null, ended.detail.ended_at]);

    const halted = record(THREAD.pacific, stopped(requestsWorld(NOW), THREAD.pacific, "demo_budget"));
    expect(propertiesOf(halted.detail, halted.open, members.helen.id).status.label).toBe("Closed");
  });

  it("says who reopened the live run, and when: a teammate by name, the counterparty by its org's", () => {
    const ours = record(THREAD.ridgeline, reopened(requestsWorld(NOW), THREAD.ridgeline));
    const run = ours.detail.runs.at(-1);
    expect(propertiesOf(ours.detail, ours.open, members.helen.id).reopened).toEqual({ by: "Helen", at: run?.started_at });
    const theirs = record(THREAD.ridgeline, reopened(requestsWorld(NOW), THREAD.ridgeline, "them"));
    expect(propertiesOf(theirs.detail, theirs.open, members.helen.id).reopened?.by).toBe("Ridgeline Fabrication");
  });
});
