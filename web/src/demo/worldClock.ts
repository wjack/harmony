import { useEffect, useMemo, useState } from "react";

import type { WorldView } from "../api/types";
import { useSession } from "../app/queries";

// Each scenario's fixed anchor date (`scenarios/<name>/scenario.yaml`). The session
// doesn't report it yet; `GET /api/demo/story`'s `world_date` does, once it's live.
const SCENARIO_ANCHORS: Readonly<Record<string, string>> = {
  "harmony-demo": "2026-10-26",
};
const DEFAULT_ANCHOR = "2026-10-26";
const DAY_MS = 86_400_000;

function parseDate(isoDate: string): Date {
  const [year = 1970, month = 1, day = 1] = isoDate.split("-").map(Number);
  return new Date(year, month - 1, day);
}

/** The world day the real instant `at` falls on: the anchor plus whole days since the world was created. */
function worldDay(world: WorldView, at: number): Date {
  const anchor = parseDate(SCENARIO_ANCHORS[world.scenario] ?? DEFAULT_ANCHOR);
  const elapsed = at - Date.parse(world.created_at) + world.ff_offset_s * 1_000;
  return new Date(anchor.getFullYear(), anchor.getMonth(), anchor.getDate() + Math.floor(elapsed / DAY_MS));
}

/**
 * The world's date (ARCHITECTURE.md §11.1): anchor + (now − world created) + fast-forward,
 * so seeded dates ("by Nov 6") stay coherent whenever the demo is opened.
 */
export function worldDate(world: WorldView, now: number = Date.now()): Date {
  // A clock a little behind the server's doesn't put the world before its anchor.
  return worldDay(world, Math.max(now, Date.parse(world.created_at)));
}

/** Where a real instant shows on the calendar people read: its date and time of day. */
export type Calendar = (at: string) => Date;

/** The real calendar: for a page with no world (the gallery's fixtures before a session). */
export const realCalendar: Calendar = (at) => new Date(at);

/**
 * The world's calendar: an instant shows on the world day it falls on, at its own time
 * of day. Seeded times are stored whole days from the world's creation (`seeds.loader`),
 * so they land on their seeded dates, beside the World date, whatever day it really is.
 */
export function worldCalendar(world: WorldView): Calendar {
  return (at) => {
    const real = new Date(at);
    const day = worldDay(world, real.getTime());
    return new Date(day.getFullYear(), day.getMonth(), day.getDate(), real.getHours(), real.getMinutes());
  };
}

/** The acting session's world calendar. */
export function useCalendar(): Calendar {
  const world = useSession().data?.world;
  return useMemo(() => (world === undefined ? realCalendar : worldCalendar(world)), [world]);
}

/** "Mon, Oct 26". */
export function formatWorldDate(date: Date): string {
  return date.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
}

/** The world date, re-read every minute so it rolls over at midnight. */
export function useWorldDate(world: WorldView | undefined): Date | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => {
      setNow(Date.now());
    }, 60_000);
    return () => {
      clearInterval(timer);
    };
  }, []);
  return world === undefined ? null : worldDate(world, now);
}
