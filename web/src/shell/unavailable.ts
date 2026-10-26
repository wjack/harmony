import { useCallback } from "react";
import { createStore, useStore } from "zustand";

interface UnavailableState {
  /** What was clicked ("Projects", "Web search"), or null when the dialog is closed. */
  feature: string | null;
}

export const unavailableStore = createStore<UnavailableState>(() => ({ feature: null }));

export function showUnavailable(feature: string): void {
  unavailableStore.setState({ feature });
}

export function closeUnavailable(): void {
  unavailableStore.setState({ feature: null });
}

/**
 * Connects anything outside the prototype's scope (UI_REFERENCE.md §10) to the one
 * "not available" dialog: `const unavailable = useUnavailable(); onClick={() => unavailable("Projects")}`.
 */
export function useUnavailable(): (feature: string) => void {
  return useCallback((feature: string) => {
    showUnavailable(feature);
  }, []);
}

export function useUnavailableFeature(): string | null {
  return useStore(unavailableStore, (state) => state.feature);
}
