/**
 * Unread dots: a thread is unread while its last activity is newer than when this
 * member last opened it in this browser. Per-browser on purpose; it's a nudge, not state.
 */
import { useCallback, useSyncExternalStore } from "react";

type SeenMap = Record<string, string>;

const listeners = new Set<() => void>();
const memo = new Map<string, { raw: string | null; map: SeenMap }>();

function storageKey(memberId: string): string {
  return `harmony.requests.seen.${memberId}`;
}

function read(memberId: string): SeenMap {
  const key = storageKey(memberId);
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(key);
  } catch {
    // Storage can be blocked (private mode); everything then reads as unread once.
  }
  const cached = memo.get(key);
  if (cached?.raw === raw) return cached.map;
  let map: SeenMap = {};
  if (raw !== null) {
    try {
      const parsed: unknown = JSON.parse(raw);
      if (typeof parsed === "object" && parsed !== null) map = parsed as SeenMap;
    } catch {
      map = {};
    }
  }
  memo.set(key, { raw, map });
  return map;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function isUnread(seen: SeenMap, threadId: string, lastActivityAt: string): boolean {
  const at = seen[threadId];
  return at === undefined || Date.parse(lastActivityAt) > Date.parse(at);
}

/** The member's seen map, and a function that marks a thread seen as of `at`. */
export function useSeen(memberId: string | undefined): {
  seen: SeenMap;
  markSeen: (threadId: string, at: string) => void;
} {
  const seen = useSyncExternalStore(
    subscribe,
    () => (memberId === undefined ? EMPTY : read(memberId)),
    () => EMPTY,
  );
  const markSeen = useCallback(
    (threadId: string, at: string) => {
      if (memberId === undefined) return;
      const current = read(memberId);
      const previous = current[threadId];
      if (previous !== undefined && Date.parse(previous) >= Date.parse(at)) return;
      try {
        window.localStorage.setItem(storageKey(memberId), JSON.stringify({ ...current, [threadId]: at }));
      } catch {
        return;
      }
      for (const listener of listeners) listener();
    },
    [memberId],
  );
  return { seen, markSeen };
}

const EMPTY: SeenMap = {};
