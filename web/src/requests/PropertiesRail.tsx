/**
 * The properties rail (UI_REFERENCE §7, Linear-style, in the card of Claude's project
 * page): status, assignee, counterparty and when the conversation expires; what our
 * representative saw; and the activity log.
 */
import { Building2, ChevronRight, CircleDot, Eye, Timer, UserRound } from "lucide-react";
import type { ReactNode } from "react";

import { useCalendar } from "../demo/worldClock";
import { firstName } from "../shell/names";
import { Avatar, OrgAvatar, Spark, cx } from "../shell/ui";
import type { ActivityEntry } from "./activity";
import { longAge, shortAge, timeLeft } from "./format";
import type { Properties } from "./properties";
import type { OrgRef, RepresentativeContext } from "./types";

/** "Its prompt and 2 released answers", or why there's nothing to show yet. */
function sawSummary(representative: RepresentativeContext | null): string {
  if (representative === null) return "Nothing yet: it hasn't taken a turn";
  const released = representative.released.length;
  if (released === 0) return "Its prompt; no answers released yet";
  return `Its prompt and ${String(released)} released ${released === 1 ? "answer" : "answers"}`;
}

interface PropertiesRailProps {
  properties: Properties;
  them: OrgRef;
  us: OrgRef;
  representative: RepresentativeContext | null;
  activity: readonly ActivityEntry[];
  onShowSaw: () => void;
}

export function PropertiesRail({ properties, them, us, representative, activity, onShowSaw }: PropertiesRailProps) {
  return (
    <div className="overflow-hidden rounded-2xl border border-border">
      <section aria-labelledby="rq-properties" className="px-4 pt-3.5 pb-3">
        <h3 id="rq-properties" className="mb-2 text-[13.5px] font-medium text-ink">
          Properties
        </h3>
        <PropertyList properties={properties} them={them} us={us} />
      </section>

      <section aria-labelledby="rq-saw" className="border-t border-border px-4 py-3.5">
        <button type="button" onClick={onShowSaw} className="group flex w-full items-start gap-3 text-left">
          <span className="min-w-0 flex-1">
            <h3 id="rq-saw" className="text-[13.5px] font-medium text-ink">
              What our representative saw
            </h3>
            <span className="mt-0.5 block text-[12.5px] leading-[18px] text-muted">{sawSummary(representative)}</span>
          </span>
          <Eye size={16} strokeWidth={1.5} aria-hidden className="mt-0.5 shrink-0 text-muted group-hover:text-ink" />
        </button>
      </section>

      <section aria-labelledby="rq-activity" className="border-t border-border px-4 pt-3.5 pb-4">
        <h3 id="rq-activity" className="mb-2.5 text-[13.5px] font-medium text-ink">
          Activity
        </h3>
        <ActivityLog entries={activity} us={us} />
      </section>
    </div>
  );
}

/** A label and its value. The rail is the only place that names the assignee and the
 * counterparty, so a long value wraps rather than being cut off. */
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
  needs: "bg-warning animate-pulse-dot",
  live: "bg-info",
  done: "bg-success",
  ended: "bg-faint",
};

export function PropertyList({ properties, them, us }: { properties: Properties; them: OrgRef; us: OrgRef }) {
  const { status, assignees, expiresAt } = properties;
  return (
    <dl className="flex flex-col">
      <Row icon={<CircleDot size={14} strokeWidth={1.5} />} label="Status">
        <span className={cx("mt-[7px] size-2 shrink-0 rounded-full", TONE_DOT[status.tone])} aria-hidden />
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

export function ActivityLog({ entries, us }: { entries: readonly ActivityEntry[]; us: OrgRef }) {
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

/** Below the rail breakpoint: the same facts as a compact strip under the title. */
export function PropertyStrip({ properties, onShowSaw }: { properties: Properties; onShowSaw: () => void }) {
  const { status } = properties;
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-[12.5px]">
      <span className="inline-flex h-7 items-center gap-1.5 rounded-full border border-border bg-surface px-2.5">
        <span className={cx("size-2 rounded-full", TONE_DOT[status.tone])} aria-hidden />
        {status.label}
      </span>
      <button
        type="button"
        onClick={onShowSaw}
        className="inline-flex h-7 items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 text-secondary hover:bg-hover"
      >
        <Eye size={13} strokeWidth={1.5} aria-hidden />
        What our representative saw
        <ChevronRight size={13} strokeWidth={1.5} aria-hidden />
      </button>
    </div>
  );
}
