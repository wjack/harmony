import { NotAvailableDialog } from "../shell/NotAvailableDialog";
import { Toaster } from "../shell/Toaster";
import { TooltipProvider } from "../shell/ui";
import { GalleryPage } from "./GalleryPage";

/**
 * /__gallery's root, rendered by main.tsx in place of the app's router: each section
 * runs its own memory router over its own fixtures, so a link or account switch inside
 * a section stays in that section instead of leaving the gallery.
 */
export function GalleryApp() {
  return (
    <TooltipProvider delayDuration={400} skipDelayDuration={200}>
      <GalleryPage />
      <Toaster />
      <NotAvailableDialog />
    </TooltipProvider>
  );
}
