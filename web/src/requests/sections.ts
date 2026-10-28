/**
 * How requests are listed (LIVE_DEMO_PLAN 3.9): the inbox's sections, the search and
 * direction filter on `/requests`, and where a row opens.
 * Pure, so the grouping and the links are unit-tested.
 */
import { escalationPath } from "../shell/escalationPlace";
import type { InboxItem, InboxSection } from "./types";

export const SECTIONS: readonly { key: InboxSection; label: string }[] = [
  { key: "needs_you", label: "Needs you" },
  { key: "with_your_team", label: "With your team" },
  { key: "claude_handling", label: "Claude is handling" },
  { key: "shared_with_you", label: "Shared with you" },
  { key: "done", label: "Done" },
];

export type Direction = "all" | "inbound" | "outbound";

export const DIRECTIONS: readonly { value: Direction; label: string }[] = [
  { value: "all", label: "All" },
  { value: "inbound", label: "Inbound" },
  { value: "outbound", label: "Outbound" },
];

/** Whether `item` shows for a search (its title or counterparty) and a direction. */
export function matches(item: InboxItem, query: string, direction: Direction): boolean {
  if (direction === "inbound" && item.side !== "responder") return false;
  if (direction === "outbound" && item.side !== "requester") return false;
  const needle = query.trim().toLowerCase();
  if (needle === "") return true;
  return item.title.toLowerCase().includes(needle) || item.counterparty.name.toLowerCase().includes(needle);
}

/**
 * Where a row opens. An item waiting on the viewer opens where it's answered, which for
 * a question their own side's representative asked is the chat that sent the request.
 * Every other row opens its thread: an item that isn't open carries the one the member
 * last resolved, and that chat's question is long settled.
 */
export function requestPath(item: InboxItem, section: InboxSection): string {
  return section === "needs_you" ? escalationPath(item) : threadPath(item);
}

/** The item's thread in Requests. */
export function threadPath(item: InboxItem): string {
  return `/requests/${encodeURIComponent(item.thread_id)}`;
}
