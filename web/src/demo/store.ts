import { createStore, useStore } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

interface DemoState {
  /** The intro opens by itself until the visitor has seen it once. */
  welcomed: boolean;
  introOpen: boolean;
  controlsOpen: boolean;
  resetConfirmOpen: boolean;
  /** Demo controls' "See user guide" asks the demo bar's "?" for "What's on screen". */
  userGuide: boolean;
  /** "See this guide any time", pointing at the "?", once the tour's guide has closed. */
  guideTip: boolean;
  /** That tip has shown in this browser: it shows once. */
  guideTipShown: boolean;
  /**
   * The first visit's handoffs, one after the other: the intro's Next asks for "What's
   * on screen" once the demo is up as Alex, and closing that asks New chat to put the
   * suggested message in its composer. Each is taken once.
   */
  introHelp: boolean;
  prefillSuggestion: boolean;
}

const STORAGE_KEY = "harmony.demo";

export const demoStore = createStore<DemoState>()(
  persist(
    (): DemoState => ({
      welcomed: false,
      introOpen: false,
      controlsOpen: false,
      resetConfirmOpen: false,
      userGuide: false,
      guideTip: false,
      guideTipShown: false,
      introHelp: false,
      prefillSuggestion: false,
    }),
    {
      name: STORAGE_KEY,
      // A storage that throws (private windows) leaves the state in memory only.
      storage: createJSONStorage(() => localStorage),
      partialize: ({ welcomed, guideTipShown }) => ({ welcomed, guideTipShown }),
    },
  ),
);

export function useDemoState<T>(selector: (state: DemoState) => T): T {
  return useStore(demoStore, selector);
}

export function openIntro(): void {
  demoStore.setState({ introOpen: true });
}

/** Closing the intro, by Next or not, counts as having seen it. */
export function closeIntro(): void {
  demoStore.setState({ introOpen: false, welcomed: true });
}

/** The intro's Next, once the demo is up as Alex: "What's on screen" next. */
export function showIntroHelp(): void {
  demoStore.setState({ introOpen: false, welcomed: true, introHelp: true });
}

/** Closing the intro's "What's on screen": the suggested message waits in New chat's composer. */
export function finishIntroHelp(): void {
  if (demoStore.getState().introHelp) demoStore.setState({ introHelp: false, prefillSuggestion: true });
}

/** New chat has put the suggested message in its composer. */
export function prefilledSuggestion(): void {
  demoStore.setState({ prefillSuggestion: false });
}

/** Demo controls' "See user guide": the modal gives way to "What's on screen". */
export function seeUserGuide(): void {
  demoStore.setState({ controlsOpen: false, userGuide: true });
}

export function closeUserGuide(): void {
  demoStore.setState({ userGuide: false });
}

/** The guide closed after the intro or Demo controls opened it: point at the "?", once. */
export function pointOutGuide(): void {
  if (!demoStore.getState().guideTipShown) demoStore.setState({ guideTip: true, guideTipShown: true });
}

export function dismissGuideTip(): void {
  demoStore.setState({ guideTip: false });
}

export function openDemoControls(): void {
  demoStore.setState({ controlsOpen: true });
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
