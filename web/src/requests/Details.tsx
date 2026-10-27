/**
 * A request's Details (LIVE_DEMO_PLAN 3.9), closed until the member opens it from the
 * header: Share, what our representative saw, the properties (Linear's, in the card of
 * Claude's project page) and the activity log. Beside the transcript when the view is
 * wide enough, else a sheet over it.
 */
import { Building2, CircleDot, Eye, Timer, UserPlus, UserRound, X } from "lucide-react";
import { Dialog } from "radix-ui";
import type { ReactNode } from "react";

import { useCalendar } from "../demo/worldClock";
import { firstName } from "../shell/names";
import { Avatar, Button, IconButton, OrgAvatar, Spark, cx } from "../shell/ui";
import type { ActivityEntry } from "./activity";
import { longAge, shortAge, timeLeft } from "./format";
import type { Properties } from "./properties";
import type { OrgRef, RepresentativeContext } from "./types";

interface DetailsBodyProps {
  properties: Properties;
  them: OrgRef;
  us: OrgRef;
  representative: RepresentativeContext | null;
  activity: readonly ActivityEntry[];
  /** `from` is the control that opened it, for focus to return to. */
  onShare: (from: HTMLElement) => void;
  onShowSaw: (from: HTMLElement) => void;
}

/** "Its prompt and 2 released answers", or why there's nothing to show yet. */
function sawSummary(representative: RepresentativeContext | null): string {
  if (representative === null) return "Nothing yet: it hasn't taken a turn";
  const released = representative.released.length;
  if (released === 0) return "Its prompt; no answers released yet";
  return `Its prompt and ${String(released)} released ${released === 1 ? "answer" : "answers"}`;
}

function DetailsBody({ properties, them, us, representative, activity, onShare, onShowSaw }: DetailsBodyProps) {
  return (
    <div className="flex flex-col gap-3">
      <Button
        variant="secondary"
        icon={<UserPlus size={15} strokeWidth={1.6} />}
        onClick={(event) => {
          onShare(event.currentTarget);
        }}
        className="self-start"
      >
        Share
      </Button>
      <div className="overflow-hidden rounded-2xl border border-border">
        <section aria-labelledby="rq-saw" className="px-4 py-3.5">
          <button
            type="button"
            onClick={(event) => {
              onShowSaw(event.currentTarget);
            }}
            className="group flex w-full items-start gap-3 text-left"
          >
            <span className="min-w-0 flex-1">
              <h3 id="rq-saw" className="text-[13.5px] font-medium text-ink">
                What our representative saw
              </h3>
              <span className="mt-0.5 block text-[12.5px] leading-[18px] text-muted">{sawSummary(representative)}</span>
            </span>
            <Eye size={16} strokeWidth={1.5} aria-hidden className="mt-0.5 shrink-0 text-muted group-hover:text-ink" />
          </button>
        </section>

        <section aria-labelledby="rq-properties" className="border-t border-border px-4 pt-3.5 pb-3">
          <h3 id="rq-properties" className="mb-2 text-[13.5px] font-medium text-ink">
            Properties
          </h3>
          <PropertyList properties={properties} them={them} us={us} />
        </section>

        <section aria-labelledby="rq-activity" className="border-t border-border px-4 pt-3.5 pb-4">
          <h3 id="rq-activity" className="mb-2.5 text-[13.5px] font-medium text-ink">
            Activity
          </h3>
          <ActivityLog entries={activity} us={us} />
        </section>
      </div>
    </div>
  );
}

interface DetailsProps extends DetailsBodyProps {
  /** A sheet over the view rather than a panel beside it. */
  sheet: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Focuses the control that opens Details, which isn't a Radix trigger: closed, focus would fall to the page. */
  returnFocus: () => void;
}

export function Details({ sheet, open, onOpenChange, returnFocus, ...body }: DetailsProps) {
  if (sheet) {
    return (
      <Dialog.Root open={open} onOpenChange={onOpenChange}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-[rgb(20_20_19/0.16)] data-[state=open]:animate-fade-in" />
          <Dialog.Content
            aria-describedby={undefined}
            onCloseAutoFocus={(event) => {
              event.preventDefault();
              returnFocus();
            }}
            className="fixed inset-y-2 right-2 z-50 flex w-[min(380px,calc(100vw-16px))] flex-col rounded-2xl border border-border bg-canvas text-ink shadow-modal outline-none data-[state=open]:animate-pop-in"
          >
            <header className="flex h-12 shrink-0 items-center justify-between pr-2 pl-4">
              <Dialog.Title className="text-[15px] font-semibold">Details</Dialog.Title>
              <Dialog.Close asChild>
                <IconButton label="Close details" size="sm">
                  <X size={16} strokeWidth={1.5} />
                </IconButton>
              </Dialog.Close>
            </header>
            <div className="scrollbar-quiet min-h-0 flex-1 overflow-y-auto px-4 pb-4">
              <DetailsBody {...body} />
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    );
  }
  if (!open) return null;
  return (
    <aside aria-label="Properties" className="flex w-[320px] shrink-0 flex-col border-l border-border bg-canvas">
      <header className="flex h-12 shrink-0 items-center justify-between border-b border-border pr-2 pl-4">
        <h2 className="text-[14px] font-medium text-ink">Details</h2>
        <IconButton
          label="Close details"
          size="sm"
          onClick={() => {
            onOpenChange(false);
            returnFocus();
          }}
        >
          <X size={16} strokeWidth={1.5} />
        </IconButton>
      </header>
      <div className="scrollbar-quiet min-h-0 flex-1 overflow-y-auto px-4 pt-4 pb-6">
        <DetailsBody {...body} />
      </div>
    </aside>
  );
}

/** A label and its value. Details is the only place that names the assignee and the
 * counterparty in full, so a long value wraps rather than being cut off. */
function Row({ icon, label, children }: { icon: ReactNode; label: string; children: ReactNode }) {
  return (
    <div className="flex items-start gap-2.5 py-[5px] text-[13px] leading-[22px]">
      <dt className="flex w-[104px] shrink-0 items-center gap-2 text-muted">
        <span className="flex size-4 items-center justify-center" aria-hidden>
          {icon}
        </span>
        {label}
      </dt>
      <dd className="flex min-w-0 flex-1 items-start gap-1.5 text-ink [overflow-wrap:anywhere]">{children}</dd>
    </div>
  );
}

const TONE_DOT: Record<Properties["status"]["tone"], string> = {
  warning: "bg-warning",
  info: "bg-info",
  success: "bg-success",
  accent: "bg-accent",
  muted: "bg-faint",
};

function PropertyList({ properties, them, us }: { properties: Properties; them: OrgRef; us: OrgRef }) {
  const { status, assignees, expiresAt } = properties;
  return (
    <dl className="flex flex-col">
      <Row icon={<CircleDot size={14} strokeWidth={1.5} />} label="Status">
        <span
          className={cx("mt-[7px] size-2 shrink-0 rounded-full", TONE_DOT[status.tone], status.needs && "animate-pulse-dot")}
          aria-hidden
        />
        <span className="min-w-0">{status.label}</span>
      </Row>
      <Row icon={<UserRound size={14} strokeWidth={1.5} />} label="Assignee">
        {assignees.length === 0 ? (
          <span className="text-muted">No one yet</span>
        ) : (
          <>
            <Avatar name={assignees[0]?.name ?? ""} seed={us.slug} size={18} className="mt-0.5 shrink-0" />
            <span className="min-w-0">
              {assignees[0]?.name}
              {assignees.length > 1 && <span className="text-muted"> +{assignees.length - 1}</span>}
            </span>
          </>
        )}
      </Row>
      <Row icon={<Building2 size={14} strokeWidth={1.5} />} label="Counterparty">
        <OrgAvatar name={them.name} seed={them.slug} size={18} className="mt-0.5 shrink-0" />
        <span className="min-w-0">{them.name}</span>
      </Row>
      {expiresAt !== null && (
        <Row icon={<Timer size={14} strokeWidth={1.5} />} label="Expires">
          <span>{timeLeft(expiresAt)}</span>
        </Row>
      )}
    </dl>
  );
}

function ActivityLog({ entries, us }: { entries: readonly ActivityEntry[]; us: OrgRef }) {
  const calendar = useCalendar();
  if (entries.length === 0) return <p className="text-[12.5px] text-muted">Nothing yet.</p>;
  return (
    <ol className="relative flex flex-col gap-2.5">
      <span aria-hidden className="absolute top-2 bottom-2 left-[8.5px] w-px bg-border" />
      {entries.map((entry) => (
        <li key={entry.id} className="relative flex items-start gap-2.5 text-[12.5px] leading-[18px]">
          {entry.actor === null ? (
            <span className="flex size-[18px] shrink-0 items-center justify-center rounded-full bg-canvas">
              <Spark size={12} />
            </span>
          ) : (
            <Avatar name={entry.actor.name} seed={us.slug} size={18} className="ring-2 ring-canvas" />
          )}
          <span className="min-w-0 flex-1 text-secondary">
            {entry.actor !== null && <span className="font-medium text-ink">{firstName(entry.actor.name)} </span>}
            {entry.text}
            <span className="text-muted" title={longAge(entry.at, calendar)}>
              {" "}
              · {shortAge(entry.at, calendar)}
            </span>
          </span>
        </li>
      ))}
    </ol>
  );
}
