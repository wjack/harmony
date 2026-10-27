/**
 * Whether the member left a request's Details open (LIVE_DEMO_PLAN 3.9): a per-member
 * convenience in this browser only. Storage may be blocked or cleared; Details then
 * starts closed, its default.
 */
import { useState } from "react";

function storageKey(memberId: string): string {
  return `harmony.requests.details.${memberId}`;
}

function stored(memberId: string | undefined): boolean {
  if (memberId === undefined) return false;
  try {
    return window.localStorage.getItem(storageKey(memberId)) === "open";
  } catch {
    return false;
  }
}

export function useDetailsOpen(memberId: string | undefined): [boolean, (open: boolean) => void] {
  // The choice made in this view, for the member who made it; until then, the stored one.
  const [choice, setChoice] = useState<{ memberId: string | undefined; open: boolean } | null>(null);
  const open = choice !== null && choice.memberId === memberId ? choice.open : stored(memberId);
  const remember = (next: boolean) => {
    setChoice({ memberId, open: next });
    if (memberId === undefined) return;
    try {
      window.localStorage.setItem(storageKey(memberId), next ? "open" : "closed");
    } catch {
      // Not remembered: the next visit starts closed.
    }
  };
  return [open, remember];
}
