import { createStore, useStore } from "zustand";

export interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface ToastInput {
  text: string;
  /** An underlined link after the text ("Chat moved to *Image analysis*"). */
  action?: ToastAction;
  /** How long it stays, in ms; null keeps it until dismissed. Default 5 s. */
  durationMs?: number | null;
  /** Replaces a live toast with the same key instead of stacking a second one. */
  key?: string;
}

export interface Toast extends ToastInput {
  id: number;
}

interface ToastState {
  toasts: Toast[];
}

const DEFAULT_DURATION_MS = 5_000;
const MAX_VISIBLE = 3;

/** Toasts live outside React and the query cache, so they survive an account switch. */
export const toastStore = createStore<ToastState>(() => ({ toasts: [] }));

const timers = new Map<number, ReturnType<typeof setTimeout>>();
let nextId = 1;

export function dismissToast(id: number): void {
  const timer = timers.get(id);
  if (timer !== undefined) clearTimeout(timer);
  timers.delete(id);
  toastStore.setState((state) => ({ toasts: state.toasts.filter((toast) => toast.id !== id) }));
}

/** Dismiss the toast shown under `key`, if it's still up. */
export function dismissToastWithKey(key: string): void {
  for (const existing of toastStore.getState().toasts) {
    if (existing.key === key) dismissToast(existing.id);
  }
}

/** Dismiss every toast whose key starts with `prefix` (e.g. all of one kind of cue). */
export function dismissToastsWithKeyPrefix(prefix: string): void {
  for (const existing of toastStore.getState().toasts) {
    if (existing.key?.startsWith(prefix) === true) dismissToast(existing.id);
  }
}

/** Show a toast (UI_REFERENCE.md §9: "Northlake replied", "Switched to Dr. Helen Ward"). */
export function toast(input: ToastInput | string): number {
  const toastInput = typeof input === "string" ? { text: input } : input;
  const id = nextId++;
  toastStore.setState((state) => {
    const kept = state.toasts.filter((existing) => {
      const replaced = toastInput.key !== undefined && existing.key === toastInput.key;
      if (replaced) dismissTimer(existing.id);
      return !replaced;
    });
    const toasts = [...kept, { ...toastInput, id }];
    for (const dropped of toasts.slice(0, Math.max(0, toasts.length - MAX_VISIBLE))) {
      dismissTimer(dropped.id);
    }
    return { toasts: toasts.slice(-MAX_VISIBLE) };
  });
  const duration = toastInput.durationMs === undefined ? DEFAULT_DURATION_MS : toastInput.durationMs;
  if (duration !== null) {
    timers.set(
      id,
      setTimeout(() => {
        dismissToast(id);
      }, duration),
    );
  }
  return id;
}

function dismissTimer(id: number): void {
  const timer = timers.get(id);
  if (timer !== undefined) clearTimeout(timer);
  timers.delete(id);
}

export function useToasts(): Toast[] {
  return useStore(toastStore, (state) => state.toasts);
}
