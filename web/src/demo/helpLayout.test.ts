import { describe, expect, it } from "vitest";

import { placeCallouts, RING, ringDistance, type Box, type CalloutSpec } from "./helpLayout";

const box = (left: number, top: number, right: number, bottom: number): Box => ({ left, top, right, bottom });

function overlaps(a: Box, b: Box): boolean {
  return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
}

/** The app shell's controls and the callouts' sizes as Chrome lays them out (the gallery's App shell, as Alex). */
function shell(targets: Record<string, Box>): CalloutSpec[] {
  const height: Record<string, number> = { "demo-controls": 92, network: 110 };
  return Object.entries(targets).map(([key, target]) => ({
    key,
    side: key === "requests" ? "right" : "below",
    extra: key === "budget" || key === "reset",
    target,
    width: 216,
    height: height[key] ?? 74,
  }));
}

const WINDOWS = [
  {
    name: "1440 × 900, the full sidebar",
    viewport: { width: 1440, height: 900 },
    specs: shell({
      "demo-controls": box(62, 7, 183, 31),
      network: box(187, 7, 303, 31),
      "acting-as": box(320, 10, 587, 28),
      budget: box(1205, 7, 1329, 31),
      reset: box(1333, 7, 1401, 31),
      requests: box(9, 236, 264, 268),
    }),
    // The "?" and the "Got it" bar.
    obstacles: [box(1405, 7, 1429, 31), box(499, 836, 941, 876)],
    placed: ["demo-controls", "network", "acting-as", "budget", "reset", "requests"],
  },
  {
    name: "900 × 700, the rail",
    viewport: { width: 900, height: 700 },
    specs: shell({
      "demo-controls": box(62, 7, 183, 31),
      network: box(187, 7, 303, 31),
      "acting-as": box(320, 10, 524, 28),
      budget: box(665, 7, 789, 31),
      reset: box(793, 7, 861, 31),
      requests: box(7, 236, 42, 268),
    }),
    obstacles: [box(865, 7, 889, 31), box(229, 636, 671, 676)],
    // Short of room, the budget's callout, an extra, gives way.
    placed: ["demo-controls", "network", "acting-as", "reset", "requests"],
  },
];

describe("placeCallouts", () => {
  it.each(WINDOWS)("keeps the callouts apart, off every control and inside $name", (shown) => {
    const { viewport, specs, obstacles, placed } = shown;
    const placements = placeCallouts(specs, viewport, obstacles);

    expect(placements.map((placement) => placement.key).sort()).toEqual([...placed].sort());
    for (const [i, { box: callout }] of placements.entries()) {
      expect(callout.left).toBeGreaterThanOrEqual(0);
      expect(callout.top).toBeGreaterThanOrEqual(0);
      expect(callout.right).toBeLessThanOrEqual(viewport.width);
      expect(callout.bottom).toBeLessThanOrEqual(viewport.height);
      for (const other of placements.slice(i + 1)) expect(overlaps(callout, other.box)).toBe(false);
      for (const { target } of specs) expect(overlaps(callout, target)).toBe(false);
      for (const obstacle of obstacles) expect(overlaps(callout, obstacle)).toBe(false);
    }
  });

  it("puts a demo-bar control's callout under it, and a sidebar row's on its right", () => {
    const placements = placeCallouts(shell({ requests: box(9, 236, 264, 268), network: box(187, 7, 303, 31) }), {
      width: 1440,
      height: 900,
    });
    const placed = (key: string) => placements.find((placement) => placement.key === key)?.box;
    expect(placed("network")?.top).toBeGreaterThan(31);
    expect(placed("requests")?.left).toBeGreaterThan(264);
  });

  it.each(WINDOWS)("keeps neighboring rings apart in $name", ({ viewport, specs, obstacles }) => {
    // The demo bar's buttons sit 4 px apart, closer than two full rings.
    const rings = placeCallouts(specs, viewport, obstacles).map((placement) => placement.ring);
    for (const [i, ring] of rings.entries()) {
      for (const other of rings.slice(i + 1)) expect(overlaps(ring, other)).toBe(false);
    }
  });
});

describe("ringDistance", () => {
  it("rings controls with room in full, and close ones less, the same for all", () => {
    expect(ringDistance([box(0, 0, 100, 24), box(120, 0, 200, 24)])).toBe(RING);
    expect(ringDistance([box(0, 0, 100, 24), box(108, 0, 200, 24)])).toBe(2);
    // Controls 4 px apart are ringed at their edges; overlapping ones too.
    expect(ringDistance([box(0, 0, 100, 24), box(104, 0, 200, 24)])).toBe(0);
    expect(ringDistance([box(0, 0, 100, 24), box(90, 0, 200, 24)])).toBe(0);
    // Far apart one way is apart, however they line up the other.
    expect(ringDistance([box(0, 0, 100, 24), box(50, 200, 150, 224)])).toBe(RING);
  });
});
