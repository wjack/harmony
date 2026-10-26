import { Check } from "lucide-react";

import { cx } from "../shell/ui";
import { participantStyle, type Tone } from "./status";
import type { ParticipantStatus } from "./types";

const PILL: Record<Tone, string> = {
  neutral: "bg-hover text-muted",
  outline: "border border-border-strong bg-surface text-muted",
  info: "bg-info-soft text-info",
  success: "bg-success-soft text-success",
  warning: "bg-warning-soft text-warning",
  muted: "bg-hover text-faint",
};

const DOT: Record<Tone, string> = {
  neutral: "bg-faint",
  outline: "bg-faint",
  info: "bg-info",
  success: "bg-success",
  warning: "bg-warning",
  muted: "bg-faint",
};

interface StatusPillProps {
  status: ParticipantStatus;
  /** "sm" for compact cards and dense lists. */
  size?: "sm" | "md";
  className?: string;
}

/** A participant's status (UI_REFERENCE.md §8): a dot, or ✓ once closed. */
export function StatusPill({ status, size = "md", className }: StatusPillProps) {
  const style = participantStyle(status);
  return (
    <span
      className={cx(
        "inline-flex shrink-0 items-center rounded-full leading-none font-medium whitespace-nowrap",
        size === "sm" ? "h-5 gap-1 px-1.5 text-[11px]" : "h-[22px] gap-1.5 px-2 text-xs",
        PILL[style.tone],
        className,
      )}
    >
      {status === "closed" ? (
        <Check size={12} strokeWidth={2.25} aria-hidden />
      ) : (
        <span aria-hidden className={cx("size-1.5 rounded-full", DOT[style.tone], style.pulse && "animate-pulse-dot")} />
      )}
      {style.label}
    </span>
  );
}
