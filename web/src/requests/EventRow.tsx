/**
 * The transcript's quiet rows (composer.html's `.event`): an icon in the avatar column,
 * then one 13px muted line that names who acted, says what happened and when, with
 * anything they typed quoted below it. The parts here are shared by every such row.
 */
import { MessageSquareText } from "lucide-react";
import type { ReactNode } from "react";

import { useCalendar } from "../demo/worldClock";
import { Tooltip } from "../shell/ui";
import { clockTime, dateTime, shortDate } from "./format";

export function EventRow({
  icon,
  label,
  header,
  children,
}: {
  icon: ReactNode;
  label: string;
  header: ReactNode;
  children?: ReactNode;
}) {
  return (
    <article aria-label={label} className="flex gap-3 text-ui-sm text-muted">
      <span aria-hidden className="flex h-[18px] w-7 shrink-0 items-center justify-center">
        {icon}
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <p>{header}</p>
        {children}
      </div>
    </article>
  );
}

/** The name that starts a row's line. */
export function Who({ children }: { children: string }) {
  return <span className="font-medium text-secondary">{children}</span>;
}

/** The row's time: the clock, or the date for a row about a whole run; the full date on hover. */
export function When({ at, date = false }: { at: string; date?: boolean }) {
  const calendar = useCalendar();
  return (
    <Tooltip content={dateTime(at, calendar)} side="top">
      <time dateTime={at} className="tabular-nums">
        {date ? shortDate(at, calendar) : clockTime(at)}
      </time>
    </Tooltip>
  );
}

/** What a member typed, in full and in quotes. */
export function Said({ children }: { children: string }) {
  return <p className="break-words whitespace-pre-wrap text-secondary">“{children}”</p>;
}

export function LinkButton({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" onClick={onClick} className="whitespace-nowrap text-link hover:underline">
      {children}
    </button>
  );
}

/** "Drafted from Alex's answer to Ridgeline, Oct 26": reuse is never silent. */
export function DraftedFrom({ line }: { line: string }) {
  return (
    <p className="flex min-w-0 items-center gap-1.5 text-ui-sm text-secondary">
      <MessageSquareText size={14} strokeWidth={1.6} aria-hidden className="shrink-0" />
      <span className="min-w-0">{line}</span>
    </p>
  );
}
