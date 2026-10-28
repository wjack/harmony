import { describe, expect, it } from "vitest";

import { THREAD, inboxFor, requestsWorld } from "../fixtures/requests";
import { members } from "../fixtures/shell";
import type { WorldView } from "../api/types";
import { realCalendar, worldCalendar } from "../demo/worldClock";
import { dateTime, endLine, needsLabel, possessive, reasonLine, rowStatus, shortAge, stateLabel, timeLeft } from "./format";

const NOW = Date.parse("2026-10-26T10:30:00Z");

describe("labels", () => {
  it("reads ages the way Linear does", () => {
    expect(shortAge(new Date(NOW - 20_000).toISOString(), realCalendar, NOW)).toBe("now");
    expect(shortAge(new Date(NOW - 4 * 60_000).toISOString(), realCalendar, NOW)).toBe("4m");
    expect(shortAge(new Date(NOW - 3 * 3_600_000).toISOString(), realCalendar, NOW)).toBe("3h");
    expect(shortAge(new Date(NOW - 2 * 86_400_000).toISOString(), realCalendar, NOW)).toBe("2d");
    // A clock a little ahead of ours never reads as negative.
    expect(shortAge(new Date(NOW + 5_000).toISOString(), realCalendar, NOW)).toBe("now");
  });

  it("says when a conversation expires as time left, which no calendar can contradict", () => {
    expect(timeLeft(new Date(NOW + 19.5 * 3_600_000).toISOString(), NOW)).toBe("in 19 hours");
    expect(timeLeft(new Date(NOW + 40 * 60_000).toISOString(), NOW)).toBe("in 40 min");
    expect(timeLeft(new Date(NOW - 60_000).toISOString(), NOW)).toBe("any moment");
  });

  it("dates rows on the world's calendar, whatever day the demo is opened", () => {
    // Made a month after the scenario's anchor date: the World date still reads Oct 26.
    const world: WorldView = {
      id: "0b1d7a3e-5c2f-4e8a-9a51-000000000001",
      scenario: "harmony-demo",
      mode: "live",
      status: "live",
      created_at: "2026-11-25T15:00:00Z",
      ff_offset_s: 0,
      skip_approvals: false,
    };
    const created = Date.parse(world.created_at);
    const calendar = worldCalendar(world);
    // Seeded 40 days before the anchor, so stored 40 days before the world was made.
    const seeded = new Date(created - 40 * 86_400_000).toISOString();
    expect(shortAge(seeded, calendar, created)).toBe("Sep 16");
    expect(shortAge(seeded, realCalendar, created)).not.toBe("Sep 16");
    // A day into the world, at the time of day it happened.
    const later = new Date(created + 26 * 3_600_000);
    expect(dateTime(later.toISOString(), calendar)).toMatch(/^Oct 27, /);
    expect(dateTime(later.toISOString(), calendar).split(", ")[1]).toBe(dateTime(later.toISOString(), realCalendar).split(", ")[1]);
  });

  it("makes possessives that read naturally", () => {
    expect(possessive("Henderson Lab")).toBe("Henderson Lab's");
    expect(possessive("Cascade Thin Films")).toBe("Cascade Thin Films'");
  });

  it("describes a thread from our side", () => {
    const ours = { side: "responder", side_to_move: "responder" } as const;
    expect(stateLabel({ ...ours, state: "active" }, "Henderson Lab")).toBe("Claude is handling");
    expect(stateLabel({ ...ours, state: "awaiting_principal" }, "Henderson Lab")).toBe("Checking with the team");
    expect(stateLabel({ state: "closed", side: "responder", side_to_move: null }, "Henderson Lab")).toBe("Closed");
  });

  it("says it's their move whether their Claude writes or waits on a person, and Closed however it ended", () => {
    const theirs = { state: "awaiting_principal", side: "responder", side_to_move: "requester" } as const;
    expect(stateLabel(theirs, "Henderson Lab")).toBe("Waiting on Henderson Lab");
    expect(stateLabel({ ...theirs, state: "active" }, "Henderson Lab")).toBe("Waiting on Henderson Lab");
    expect(stateLabel({ ...theirs, state: "expired", side_to_move: null }, "Henderson Lab")).toBe("Closed");
  });

  it("says what an open item asks of the member", () => {
    expect(needsLabel("consult")).toBe("Needs your answer");
    expect(needsLabel("outbound_review")).toBe("Reply needs your review");
  });

  it("says whose an open item is in the row subtitle", () => {
    const world = requestsWorld(NOW);
    const helens = inboxFor(world, members.helen).needs_you.find((item) => item.thread_id === THREAD.henderson);
    const rajs = inboxFor(world, members.raj).shared_with_you.find((item) => item.thread_id === THREAD.henderson);
    expect(helens && rowStatus(helens, true)).toBe("Needs your answer");
    // Raj sees Helen's item because she shared it, not because it needs him.
    expect(rajs && rowStatus(rajs, false)).toBe("Shared by Helen");
    const closed = inboxFor(world, members.helen).done.find((item) => item.thread_id === THREAD.survey);
    expect(closed && rowStatus(closed, false)).toBe("Closed");
  });

  it("says a shared thread stopped or ended before saying who shared it", () => {
    const world = requestsWorld(NOW);
    const shared = inboxFor(world, members.raj).shared_with_you.find((item) => item.thread_id === THREAD.henderson);
    if (shared === undefined) throw new Error("Raj has Helen's shared thread");
    const settled = { ...shared, escalation: null };
    expect(rowStatus({ ...settled, thread_state: "closed", side_to_move: null }, false)).toBe("Closed");
    expect(rowStatus({ ...settled, thread_state: "expired", side_to_move: null }, false)).toBe("Closed");
    expect(rowStatus({ ...settled, stop_reason: "paused" }, false)).toBe("Closed");
    expect(rowStatus(settled, false)).toBe("Shared by Helen");
  });
});

describe("why a run ended", () => {
  const ending = (endReason: Parameters<typeof reasonLine>[0]["endReason"], closedBy: "requester" | "responder" | null = null) => ({
    endReason,
    closedBy,
    stopReason: null,
  });

  it("names who closed it from our side: our own Claude, or the counterparty", () => {
    expect(endLine(ending("closed", "requester"), "responder", "Vantage Market Intelligence", "ok")).toBe(
      "Vantage Market Intelligence closed it",
    );
    expect(endLine(ending("closed", "responder"), "responder", "Vantage Market Intelligence", "ok")).toBe("Claude closed it");
    expect(endLine(ending("closed", "requester"), "requester", "Northlake", "ok")).toBe("Claude closed it");
    // A seeded close names nobody.
    expect(endLine(ending("closed"), "responder", "Vantage Market Intelligence", "ok")).toBeNull();
  });

  it("gives the reason for each way a run ends without a close", () => {
    expect(endLine(ending("quiet"), "responder", "Henderson Lab", "ok")).toBe("both sides went quiet");
    expect(endLine(ending("unsent"), "requester", "Henderson Lab", "ok")).toBe("nothing was sent");
    expect(endLine(ending("turn_limit"), "responder", "Henderson Lab", "ok")).toBe("it reached its turn limit");
    expect(endLine(ending("deadline"), "requester", "Henderson Lab", "ok")).toBe("it reached its deadline");
    expect(endLine(ending(null), "responder", "Henderson Lab", "ok")).toBeNull();
  });

  it("says why the system stopped it, from what the budget says now", () => {
    const stopped = { endReason: "stopped" as const, closedBy: null, stopReason: "demo_budget" as const };
    expect(endLine(stopped, "responder", "Henderson Lab", "demo_exhausted")).toBe("The demo's live-model budget ran out.");
    expect(endLine({ ...stopped, stopReason: "refused" }, "requester", "Henderson Lab", "ok")).toBe(
      "Claude declined to continue this conversation.",
    );
    expect(reasonLine({ endReason: "stopped", stopReason: null }, null, "ok")).toBeNull();
  });
});
