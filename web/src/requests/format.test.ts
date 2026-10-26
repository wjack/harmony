import { describe, expect, it } from "vitest";

import { THREAD, inboxFor, requestsWorld } from "../fixtures/requests";
import { members } from "../fixtures/shell";
import type { WorldView } from "../api/types";
import { realCalendar, worldCalendar } from "../demo/worldClock";
import { dateTime, needsLabel, possessive, rowStatus, shortAge, stateLabel, timeLeft } from "./format";

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

  it("says a consult on the other side is theirs", () => {
    const theirs = { state: "awaiting_principal", side: "responder", side_to_move: "requester" } as const;
    expect(stateLabel(theirs, "Henderson Lab")).toBe("Waiting on Henderson Lab");
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
});
