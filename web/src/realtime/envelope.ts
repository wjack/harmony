import type { EventEnvelope } from "../api/types";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Parse one SSE frame's `data` into an `EventEnvelope`, or null if it isn't one.
 *
 * Only the fields the dispatcher routes on are checked; the payload's shape is the
 * server's typed contract. An event `type` this build doesn't know passes through, so
 * a newer server never breaks an open tab: the dispatcher ignores what it can't route.
 */
export function parseEnvelope(raw: string): EventEnvelope | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(value) || !isRecord(value.event) || !isRecord(value.scope)) return null;
  if (typeof value.world_id !== "string" || typeof value.event.type !== "string") return null;
  const { seq } = value;
  if (seq !== null && !(typeof seq === "number" && Number.isSafeInteger(seq) && seq >= 1)) {
    return null;
  }
  return value as EventEnvelope;
}
