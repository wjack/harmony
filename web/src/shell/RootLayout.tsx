import { Outlet } from "react-router";

import { DemoCues } from "../demo/DemoCues";
import { NotAvailableDialog } from "./NotAvailableDialog";
import { Toaster } from "./Toaster";
import { TooltipProvider } from "./ui";

/**
 * The root route: what every page shares and what must outlive an account switch
 * (toasts, the not-available dialog, the demo cue subscription).
 */
export function RootLayout() {
  return (
    <TooltipProvider delayDuration={400} skipDelayDuration={200}>
      <Outlet />
      <Toaster />
      <NotAvailableDialog />
      <DemoCues />
    </TooltipProvider>
  );
}
