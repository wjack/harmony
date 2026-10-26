import { useEffect, useSyncExternalStore } from "react";

import { showRail } from "../shell/sidebarStore";

// Below this width the side panel beside the full sidebar leaves the canvas too narrow
// for the story's graph at a readable zoom (geometry.ts `READABLE_ZOOM`).
const NARROW_WINDOW = "(max-width: 1199px)";

function query(): MediaQueryList | null {
  return typeof window.matchMedia === "function" ? window.matchMedia(NARROW_WINDOW) : null;
}

function subscribe(onChange: () => void): () => void {
  const list = query();
  list?.addEventListener("change", onChange);
  return () => {
    list?.removeEventListener("change", onChange);
  };
}

/**
 * While the org panel is open on a narrow window, the app sidebar shows its icon rail,
 * so the canvas keeps the room its graph needs; it comes back when the panel closes.
 */
export function useRailWhilePanelOpen(panelOpen: boolean): void {
  const narrow = useSyncExternalStore(
    subscribe,
    () => query()?.matches ?? false,
    () => false,
  );
  useEffect(() => {
    if (!panelOpen || !narrow) return;
    return showRail();
  }, [panelOpen, narrow]);
}
