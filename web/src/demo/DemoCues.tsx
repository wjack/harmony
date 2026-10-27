import { useDemoCues, useFollowSwitch } from "./accounts";

/**
 * Subscribes the tab to `demo.needs_human` cues (toasts and demo-bar chips) and has it
 * follow every account switch. Renders nothing.
 */
export function DemoCues() {
  useDemoCues();
  useFollowSwitch();
  return null;
}
