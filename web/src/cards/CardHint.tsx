import { Info, Sparkles } from "lucide-react";
import type { ReactNode } from "react";

import { cx } from "../shell/ui";

export interface CardHintProps {
  /**
   * "reason": the muted line under a card's title saying why it waits. "unsent_edit":
   * the blue line saying an instruction to Claude isn't sent yet (blue is your Claude).
   */
  tone: "reason" | "unsent_edit";
  children: ReactNode;
}

export function CardHint({ tone, children }: CardHintProps) {
  const Icon = tone === "reason" ? Info : Sparkles;
  return (
    <p
      className={cx(
        "flex items-start gap-1.5",
        tone === "reason" ? "text-ui-sm text-muted" : "text-ui-xs text-link",
      )}
    >
      <Icon size={tone === "reason" ? 15 : 13} strokeWidth={1.75} aria-hidden className="mt-px shrink-0" />
      <span className="min-w-0">{children}</span>
    </p>
  );
}
