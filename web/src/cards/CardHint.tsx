import { Info } from "lucide-react";
import type { ReactNode } from "react";

export interface CardHintProps {
  children: ReactNode;
}

/** The muted line under a card's title saying why it waits. */
export function CardHint({ children }: CardHintProps) {
  return (
    <p className="flex items-start gap-1.5 text-ui-sm text-muted">
      <Info size={15} strokeWidth={1.75} aria-hidden className="mt-px shrink-0" />
      <span className="min-w-0">{children}</span>
    </p>
  );
}
