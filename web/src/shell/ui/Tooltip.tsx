import { Tooltip as RadixTooltip } from "radix-ui";
import type { ReactElement, ReactNode } from "react";

export const TooltipProvider = RadixTooltip.Provider;

interface TooltipProps {
  content: ReactNode;
  /** The trigger; it must forward refs and props (a button, a link). */
  children: ReactElement;
  side?: "top" | "right" | "bottom" | "left";
  /** Render the trigger without a tooltip (e.g. the label is already visible). */
  disabled?: boolean;
}

/** Claude's dark tooltip pill. Needs a `<TooltipProvider>` above it (the app root has one). */
export function Tooltip({ content, children, side = "right", disabled = false }: TooltipProps) {
  if (disabled) return children;
  return (
    <RadixTooltip.Root>
      <RadixTooltip.Trigger asChild>{children}</RadixTooltip.Trigger>
      <RadixTooltip.Portal>
        <RadixTooltip.Content
          side={side}
          sideOffset={6}
          className="z-50 rounded-[6px] bg-tooltip px-2 py-1 text-xs leading-4 font-medium text-on-tooltip shadow-menu data-[state=delayed-open]:animate-fade-in"
        >
          {content}
        </RadixTooltip.Content>
      </RadixTooltip.Portal>
    </RadixTooltip.Root>
  );
}
