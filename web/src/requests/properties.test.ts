import { describe, expect, it } from "vitest";

import { THREAD, requestsWorld } from "../fixtures/requests";
import { members } from "../fixtures/shell";
import { propertiesOf } from "./properties";
import type { ThreadDetail } from "./types";

const NOW = Date.parse("2026-10-26T10:30:00Z");

function record(threadId: string) {
  const found = requestsWorld(NOW).threads.get(threadId);
  if (found === undefined) throw new Error(`no thread ${threadId}`);
  const detail: ThreadDetail = { ...found.detail, access_reason: "routed", access_reasons: ["routed"] };
  const open = found.workspace.escalations.find((escalation) => escalation.status === "open") ?? null;
  return { detail, open };
}

describe("propertiesOf", () => {
  it("names who an open item needs, as you when it's yours", () => {
    const { detail, open } = record(THREAD.henderson);
    expect(propertiesOf(detail, open, members.helen.id).status).toEqual({ label: "Needs you", tone: "needs" });
    expect(propertiesOf(detail, open, members.raj.id).status).toEqual({ label: "Needs Helen", tone: "needs" });
    // The conversation's timeout, not a date anyone asked for: the rail says when it expires.
    expect(propertiesOf(detail, open, members.helen.id).expiresAt).toBe(detail.deadline_at);
  });

  it("falls back to the thread's state and routed members when nothing is open", () => {
    const live = record(THREAD.calder);
    const properties = propertiesOf(live.detail, live.open, members.helen.id);
    expect(properties.status).toEqual({ label: "Claude is handling", tone: "live" });
    expect(properties.assignees.map((assignee) => assignee.slug)).toEqual(["raj"]);
    const ended = record(THREAD.ridgeline);
    expect(propertiesOf(ended.detail, ended.open, members.helen.id).status).toEqual({ label: "Closed", tone: "done" });
    // An ended conversation has nothing left to expire.
    expect(propertiesOf(ended.detail, ended.open, members.helen.id).expiresAt).toBeNull();
  });
});
