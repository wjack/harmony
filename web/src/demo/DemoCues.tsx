import { useDemoCues } from "./accounts";

/** Subscribes the tab to `demo.needs_human` cues (toasts and demo-bar chips). Renders nothing. */
export function DemoCues() {
  useDemoCues();
  return null;
}
