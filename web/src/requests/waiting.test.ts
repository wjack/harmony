import { describe, expect, it } from "vitest";

import { ESCALATION, THREAD, checking, requestsWorld } from "../fixtures/requests";
import { members, orgs } from "../fixtures/shell";
import { waitingLine, type WaitingInput } from "./waiting";

const NOW = Date.parse("2026-10-26T10:30:00Z");

function input(threadId: string, world = requestsWorld(NOW)): WaitingInput {
  const record = world.threads.get(threadId);
  if (record === undefined) throw new Error(`no fixture thread ${threadId}`);
  return {
    thread: record.detail,
    typing: false,
    consults: record.workspace.consults,
    escalation: record.workspace.escalations.find((item) => item.status === "open") ?? null,
    viewerId: members.helen.id,
    us: orgs.northlake,
    them: record.detail.requester,
  };
}

describe("waitingLine", () => {
  it("says it's their move, whatever their Claude is doing, until ours writes", () => {
    const pacific = input(THREAD.pacific);
    const theirs = { ...pacific, thread: { ...pacific.thread, state: "awaiting_principal" as const, side_to_move: "requester" as const } };
    expect(waitingLine(theirs)).toBe("Waiting for Pacific Beamline to reply");
    expect(waitingLine({ ...theirs, thread: { ...theirs.thread, state: "active" } })).toBe(
      "Waiting for Pacific Beamline to reply",
    );
    // Never beside our own typing row.
    expect(waitingLine({ ...theirs, typing: true })).toBeNull();
  });

  it("says our Claude is checking our records while the answerer works", () => {
    expect(waitingLine(input(THREAD.pacific, checking(requestsWorld(NOW), THREAD.pacific)))).toBe(
      "Checking Northlake Characterization Core's records…",
    );
  });

  it("names whose answer it waits on for anyone but the assignee, who has the card", () => {
    const henderson = input(THREAD.henderson);
    expect(henderson.escalation?.id).toBe(ESCALATION.henderson);
    expect(waitingLine(henderson)).toBeNull();
    expect(waitingLine({ ...henderson, viewerId: members.raj.id })).toBe("Waiting on Dr. Helen Ward");
  });

  it("says nothing once the conversation is closed", () => {
    expect(waitingLine(input(THREAD.ridgeline))).toBeNull();
  });
});
