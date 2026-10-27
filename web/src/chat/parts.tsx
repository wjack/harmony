/** Small pieces the chat's cards share: org monograms, fit badges, status pills, the tool card frame. */
import type { ReactNode } from "react";

import { OrgAvatar as Monogram } from "../shell/ui/Avatar";
import { cx } from "../shell/ui/cx";
import type { Fit, OrgRef } from "./types";

/** An org's monogram, as the inbox and the network view draw it. */
export function OrgAvatar({ org, size = 24 }: { org: Pick<OrgRef, "slug" | "name">; size?: number }) {
  return <Monogram name={org.name} seed={org.slug} size={size} />;
}

const FIT_LABEL: Record<Fit, string> = {
  strong: "Strong fit",
  possible: "Possible fit",
  no: "Not a fit",
  unknown: "Fit unknown",
};

const FIT_STYLE: Record<Fit, string> = {
  strong: "bg-success-soft text-success",
  possible: "bg-warning-soft text-warning",
  no: "bg-bubble text-muted",
  unknown: "bg-bubble text-muted",
};

export function FitBadge({ fit }: { fit: Fit | null }) {
  if (fit === null) return null;
  return (
    <span className={cx("inline-flex h-5 items-center rounded-full px-2 text-[11.5px] font-medium", FIT_STYLE[fit])}>
      {FIT_LABEL[fit]}
    </span>
  );
}

export type PillTone = "info" | "success" | "warning" | "muted" | "accent";

const PILL_STYLE: Record<PillTone, string> = {
  info: "bg-info-soft text-info",
  success: "bg-success-soft text-success",
  warning: "bg-warning-soft text-warning",
  accent: "bg-accent-soft text-accent-strong",
  muted: "bg-bubble text-muted",
};

const DOT_STYLE: Record<PillTone, string> = {
  info: "bg-info",
  success: "bg-success",
  warning: "bg-warning",
  accent: "bg-accent",
  muted: "bg-faint",
};

export function Pill({
  tone,
  children,
  pulse = false,
  icon,
  title,
  shrinks = false,
}: {
  tone: PillTone;
  children: ReactNode;
  pulse?: boolean;
  icon?: ReactNode;
  title?: string;
  /** Gives way to its neighbours in a crowded row: its children truncate, so `title` should carry the label whole. */
  shrinks?: boolean;
}) {
  return (
    <span
      title={title}
      className={cx(
        "inline-flex h-6 items-center gap-1.5 rounded-full px-2.5 text-[12.5px] font-medium whitespace-nowrap",
        shrinks ? "min-w-0" : "shrink-0",
        PILL_STYLE[tone],
      )}
    >
      {icon ?? <span className={cx("size-1.5 shrink-0 rounded-full", DOT_STYLE[tone], pulse && "animate-pulse-dot")} />}
      {children}
    </span>
  );
}

/**
 * Claude's tool result card ("Connector search · 10 connectors"): a hairline box on the
 * canvas with an icon, a title and right-aligned meta.
 */
export function ToolCard({
  icon,
  title,
  meta,
  children,
  footer,
  className,
  labelledBy,
}: {
  icon: ReactNode;
  title: ReactNode;
  meta?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  className?: string;
  labelledBy?: string;
}) {
  return (
    <section aria-labelledby={labelledBy} className={cx("rounded-[10px] border border-border", className)}>
      <header className="flex min-h-11 items-center gap-2.5 px-4 pt-2 pb-1.5">
        <span className="text-secondary">{icon}</span>
        <h3 id={labelledBy} className="min-w-0 flex-1 truncate text-[15px] text-ink">
          {title}
        </h3>
        {meta !== undefined && <span className="shrink-0 text-[13px] text-muted">{meta}</span>}
      </header>
      {children !== undefined && <div className="px-4 pb-3">{children}</div>}
      {footer !== undefined && <footer className="border-t border-border px-4 py-2">{footer}</footer>}
    </section>
  );
}
