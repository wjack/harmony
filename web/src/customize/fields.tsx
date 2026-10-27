/**
 * The form pieces Customize → Harmony is built from, in Claude's settings style
 * (UI_REFERENCE.md §6): 15px semibold section headings over hairline dividers, 13px
 * labels with muted hints, and white inputs with an 8px radius.
 */
import { useId, type ReactNode, type TextareaHTMLAttributes, type InputHTMLAttributes } from "react";

import { cx } from "../shell/ui";

/**
 * A control a viewer can't change (a non-admin's pane): no white fill or strong border,
 * so it reads as a value rather than an input.
 */
export const DISABLED_CONTROL =
  "disabled:cursor-default disabled:border-border disabled:bg-transparent disabled:text-secondary";

const FIELD = cx(
  "w-full rounded-control border border-border-strong bg-surface px-3 text-sm leading-5 text-ink transition-colors outline-none placeholder:text-faint focus:border-accent focus:ring-2 focus:ring-accent/15 focus-visible:outline-none disabled:placeholder:text-transparent",
  DISABLED_CONTROL,
);

interface SectionProps {
  title: ReactNode;
  description?: ReactNode;
  /** Right of the heading: a count, a dropdown. */
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
}

/** One block of a pane, divided from the next by a hairline. */
export function Section({ title, description, aside, children, className }: SectionProps) {
  return (
    <section className={cx("border-t border-border py-6 first:border-t-0 first:pt-2", className)}>
      <div className="mb-4 flex items-start gap-4">
        <div className="min-w-0 flex-1">
          <h3 className="text-[15px] leading-6 font-semibold text-ink">{title}</h3>
          {description !== undefined && <p className="mt-0.5 text-[13px] leading-5 text-muted">{description}</p>}
        </div>
        {aside !== undefined && <div className="flex shrink-0 items-center gap-2">{aside}</div>}
      </div>
      {children}
    </section>
  );
}

interface FieldProps {
  label: ReactNode;
  hint?: ReactNode;
  /** Right of the label: a character count, a link. */
  aside?: ReactNode;
  children: (id: string) => ReactNode;
  className?: string;
}

/** A labelled control; the render prop receives the id the label points at. */
export function Field({ label, hint, aside, children, className }: FieldProps) {
  const id = useId();
  return (
    <div className={cx("flex flex-col gap-1.5", className)}>
      <div className="flex items-baseline gap-3">
        <label htmlFor={id} className="min-w-0 flex-1 text-[13px] leading-5 font-medium text-ink">
          {label}
        </label>
        {aside}
      </div>
      {children(id)}
      {hint !== undefined && <p className="text-[13px] leading-5 text-muted">{hint}</p>}
    </div>
  );
}

export function TextInput({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input type="text" className={cx(FIELD, "h-9", className)} {...props} />;
}

export function TextArea({ className, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cx(FIELD, "scrollbar-quiet block resize-y py-2 leading-[22px] disabled:resize-none", className)} {...props} />;
}

/** "1,240 / 20,000", turning red past the limit. */
export function CharCount({ value, max }: { value: string; max: number }) {
  return (
    <span className={cx("text-xs tabular-nums", value.length > max ? "text-danger" : "text-faint")}>
      {value.length.toLocaleString("en-US")} / {max.toLocaleString("en-US")}
    </span>
  );
}

/** A small grey tag beside a title, like Claude's "Interactive" on a connector. */
export function Tag({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span
      className={cx(
        "inline-flex h-5 shrink-0 items-center rounded-[5px] bg-hover px-1.5 text-[11px] leading-none font-medium text-secondary",
        className,
      )}
    >
      {children}
    </span>
  );
}

/** A count badge after a group's name ("Read-only tools 11"). */
export function Count({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex h-4 min-w-4 items-center justify-center rounded-[4px] bg-hover px-1 text-[10px] leading-none font-medium text-muted tabular-nums">
      {children}
    </span>
  );
}

type CalloutTone = "neutral" | "warning";

/** A tinted note inside a pane: the view-only banner, the Skip warning. */
export function Callout({
  tone = "neutral",
  icon,
  children,
  className,
}: {
  tone?: CalloutTone;
  icon?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      role={tone === "warning" ? "alert" : "note"}
      className={cx(
        "flex gap-2.5 rounded-control px-3 py-2.5 text-[13px] leading-5",
        tone === "warning" ? "bg-warning-soft text-ink" : "border border-border bg-surface text-secondary",
        className,
      )}
    >
      {icon !== undefined && (
        <span className={cx("mt-px shrink-0", tone === "warning" ? "text-warning" : "text-muted")}>{icon}</span>
      )}
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}
