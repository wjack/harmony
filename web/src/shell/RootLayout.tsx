import { Outlet } from "react-router";

import { useRealtime } from "../app/context";
import { DemoCues } from "../demo/DemoCues";
import { NotAvailableDialog } from "./NotAvailableDialog";
import { Toaster } from "./Toaster";
import { TooltipProvider } from "./ui";

/**
 * The root route: what every page shares and what must outlive an account switch
 * (toasts, the not-available dialog, the demo cue subscription).
 */
export function RootLayout() {
  // A request in flight as the tab locks may still fail with a toast; it isn't shown.
  const locked = useRealtime((state) => state.status === "locked");
  return (
    <TooltipProvider delayDuration={400} skipDelayDuration={200}>
      <Outlet />
      {!locked && <Toaster />}
      <NotAvailableDialog />
      <DemoCues />
    </TooltipProvider>
  );
}
