import type { ReactNode } from "react";

/**
 * The card's foot: the card's last child, sticking to the bottom of the scroll area
 * while the card is taller than the view, so its buttons stay in reach.
 */
export function FootShell({ children }: { children: ReactNode }) {
  return (
    <div
      data-card-foot
      className="sticky bottom-0 z-[2] rounded-b-[inherit] border-t border-border bg-surface px-4 py-3 shadow-[0_-10px_14px_-12px_rgb(20_20_19/0.18)]"
    >
      {children}
    </div>
  );
}
