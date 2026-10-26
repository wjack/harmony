import { createStore, useStore } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

import type { DemoStory } from "../api/types";

/** The visitor's walkthrough preferences (ARCHITECTURE.md §11.5); kept in this browser. */
export interface WalkthroughPrefs {
  /** Show the suggested opening message as a chip under Alex's composer. */
  showSuggestedMessage: boolean;
  /** Ring and tag the story's suggested recipients in the network view. */
  highlightSuggestedRecipients: boolean;
}

export type DemoControlsTab = "overview" | "controls";

interface DemoState extends WalkthroughPrefs {
  /** The demo controls modal opens by itself until the visitor has seen it once. */
  welcomed: boolean;
  controlsOpen: boolean;
  controlsTab: DemoControlsTab;
  resetConfirmOpen: boolean;
}

const STORAGE_KEY = "harmony.demo";

export const demoStore = createStore<DemoState>()(
  persist(
    (): DemoState => ({
      showSuggestedMessage: true,
      highlightSuggestedRecipients: true,
      welcomed: false,
      controlsOpen: false,
      controlsTab: "overview",
      resetConfirmOpen: false,
    }),
    {
      name: STORAGE_KEY,
      // A storage that throws (private windows) leaves the state in memory only.
      storage: createJSONStorage(() => localStorage),
      partialize: ({ showSuggestedMessage, highlightSuggestedRecipients, welcomed }) => ({
        showSuggestedMessage,
        highlightSuggestedRecipients,
        welcomed,
      }),
    },
  ),
);

export function useDemoState<T>(selector: (state: DemoState) => T): T {
  return useStore(demoStore, selector);
}

/** The visitor's walkthrough switches. `useDemoStory()` already applies them. */
export function useWalkthroughPrefs(): WalkthroughPrefs {
  const showSuggestedMessage = useDemoState((state) => state.showSuggestedMessage);
  const highlightSuggestedRecipients = useDemoState((state) => state.highlightSuggestedRecipients);
  return { showSuggestedMessage, highlightSuggestedRecipients };
}

/**
 * The story as the visitor chose to see it: no suggested message (so no chip under the
 * composer) or no suggested recipients (so no rings in the network view) when the
 * matching switch in Demo controls is off.
 */
export function applyWalkthroughPrefs(story: DemoStory, prefs: WalkthroughPrefs): DemoStory {
  if (prefs.showSuggestedMessage && prefs.highlightSuggestedRecipients) return story;
  return {
    ...story,
    suggested_message: prefs.showSuggestedMessage ? story.suggested_message : null,
    suggested_recipients: prefs.highlightSuggestedRecipients ? story.suggested_recipients : [],
  };
}

export function setWalkthroughPref(key: keyof WalkthroughPrefs, value: boolean): void {
  demoStore.setState({ [key]: value });
}

export function openDemoControls(tab?: DemoControlsTab): void {
  demoStore.setState(tab === undefined ? { controlsOpen: true } : { controlsOpen: true, controlsTab: tab });
}

export function setDemoControlsTab(tab: DemoControlsTab): void {
  demoStore.setState({ controlsTab: tab });
}

/** Closing the modal also counts as having seen it, so it stops opening by itself. */
export function closeDemoControls(): void {
  demoStore.setState({ controlsOpen: false, welcomed: true });
}

export function openResetConfirm(): void {
  demoStore.setState({ resetConfirmOpen: true });
}

export function closeResetConfirm(): void {
  demoStore.setState({ resetConfirmOpen: false });
}
