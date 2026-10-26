import { useSyncExternalStore } from "react";

/**
 * A shared, coarse clock for "waiting 4m" labels: one interval for every subscriber,
 * read through useSyncExternalStore so render stays pure.
 */
const TICK_MS = 15_000;

let current = Date.now();
let timer: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) {
    current = Date.now();
    timer = setInterval(() => {
      current = Date.now();
      for (const notify of listeners) notify();
    }, TICK_MS);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

function snapshot(): number {
  return current;
}

/** Now, in ms, refreshed every 15 s while anything is subscribed. */
export function useNow(): number {
  return useSyncExternalStore(subscribe, snapshot);
}
