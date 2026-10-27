import { describe, expect, it } from "vitest";

import type { EventEnvelope } from "../api/types";
import { affectedThreads, endsTyping } from "./live";

const scope: EventEnvelope["scope"] = { kind: "org", org_id: "org", member_id: null, thread_id: null, session_id: null };

function envelope(event: EventEnvelope["event"]): EventEnvelope {
  return { world_id: "world", seq: 1, created_at: "2026-10-26T10:30:00Z", scope, event };
}

function snapshot(entityType: string, entityId: string, data: Record<string, unknown>): EventEnvelope {
  return envelope({ type: "entity.snapshot", entity_type: entityType, entity_id: entityId, version: 2, data });
}

describe("affectedThreads", () => {
  it("refreshes a thread and the inbox when one of its entities changes", () => {
    expect(affectedThreads(snapshot("thread", "t1", {}))).toEqual({ inbox: true, threads: ["t1"] });
    expect(affectedThreads(snapshot("internal_note", "n1", { thread_id: "t2" }))).toEqual({
      inbox: true,
      threads: ["t2"],
    });
  });

  it("treats a snapshot without a thread id as possibly the open thread's", () => {
    expect(affectedThreads(snapshot("escalation_draft", "d1", {}))).toEqual({ inbox: true, threads: [null] });
  });

  it("refreshes a thread whose access changed", () => {
    const shared = envelope({
      type: "thread.access_changed",
      thread_id: "t3",
      member_id: "m1",
      reason: "shared",
      added: true,
      shared_by: "m2",
      question: null,
    });
    expect(affectedThreads(shared)).toEqual({ inbox: true, threads: ["t3"] });
  });

  it("refreshes the thread when one of our edits or Claude's guidance changes", () => {
    expect(affectedThreads(snapshot("steer", "s1", { thread_id: "t4" }))).toEqual({ inbox: true, threads: ["t4"] });
    expect(affectedThreads(snapshot("rep_guidance", "g1", { thread_id: "t4" }))).toEqual({ inbox: true, threads: ["t4"] });
  });

  it("ignores entities the inbox doesn't show", () => {
    expect(affectedThreads(snapshot("conversation", "c1", {}))).toEqual({ inbox: false, threads: [] });
  });
});

describe("endsTyping", () => {
  it("ends on the thread's committed message or when the thread ends", () => {
    expect(endsTyping(snapshot("thread_message", "m1", { thread_id: "t1" }), "t1")).toBe(true);
    expect(endsTyping(snapshot("thread_message", "m2", { thread_id: "t2" }), "t1")).toBe(false);
    expect(endsTyping(snapshot("thread", "t1", { state: "closed" }), "t1")).toBe(true);
    expect(endsTyping(snapshot("thread", "t1", { state: "active" }), "t1")).toBe(false);
    expect(endsTyping(snapshot("thread", "t2", { state: "closed" }), "t1")).toBe(false);
  });

  it("ends when our Claude goes to ask the team, on this thread only", () => {
    expect(endsTyping(snapshot("consult", "c1", { thread_id: "t1" }), "t1")).toBe(true);
    expect(endsTyping(snapshot("escalation", "e1", { thread_id: "t1" }), "t1")).toBe(true);
    expect(endsTyping(snapshot("consult", "c2", { thread_id: "t2" }), "t1")).toBe(false);
    expect(endsTyping(snapshot("internal_note", "n1", { thread_id: "t1" }), "t1")).toBe(false);
  });
});
