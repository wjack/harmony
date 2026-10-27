import { Check, ChevronRight, CircleAlert, Waypoints } from "lucide-react";
import { useId } from "react";
import { Link } from "react-router";

import type { BudgetState } from "../../app/budget";
import { useCalendar } from "../../demo/worldClock";
import { useRequestStatus } from "../api";
import { screenedLine } from "../brief";
import { stoppedLine, useBudgetState } from "../failureCopy";
import { formatSince } from "../format";
import { useRequestLive } from "../live";
import { OrgAvatar, Pill, ToolCard } from "../parts";
import type { RecipientStatus, RequestStatusView } from "../types";

function RecipientPill({ recipient, memberId }: { recipient: RecipientStatus; memberId: string | null }) {
  const calendar = useCalendar();
  // Listed on the card but left out of the request (3.13 E4(f)).
  if (!recipient.sent) return <Pill tone="muted">Not sent</Pill>;
  switch (recipient.participant) {
    case "screened":
      return <Pill tone="muted">Screened</Pill>;
    case "messaged":
      return <Pill tone="info">Messaged</Pill>;
    case "replied":
      return <Pill tone="success">Replied</Pill>;
    case "needs_human": {
      const waiting = recipient.waiting_on;
      if (waiting !== null && waiting.member.id === memberId) {
        return (
          <Pill tone="accent" pulse title={`Waiting on you since ${formatSince(waiting.since, calendar)}`}>
            Needs your answer
          </Pill>
        );
      }
      return (
        <Pill
          tone="warning"
          pulse
          title={waiting === null ? undefined : `Waiting on ${waiting.member.name} · ${formatSince(waiting.since, calendar)}`}
        >
          Needs human
        </Pill>
      );
    }
    case "closed":
      return (
        <Pill tone="muted" icon={<Check size={12} strokeWidth={2.4} />}>
          Closed
        </Pill>
      );
  }
}

/** What to show under the name: our closing summary once it's closed, else the newest line. */
function recipientLine(recipient: RecipientStatus): string | null {
  return recipient.participant === "closed"
    ? (recipient.close_summary ?? recipient.latest_line)
    : recipient.latest_line;
}

function RecipientRow({
  recipient,
  memberId,
  budget,
}: {
  recipient: RecipientStatus;
  memberId: string | null;
  budget: BudgetState;
}) {
  const stopped = recipient.stop_reason ?? null;
  // A stopped thread shows only its summary: our side writes one in its last turn when
  // the other side refused; a failed or capped thread never gets that turn.
  const line = stopped === null ? recipientLine(recipient) : recipient.close_summary;
  const body = (
    <>
      <OrgAvatar org={recipient.org} size={22} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[14px] text-ink">{recipient.org.name}</span>
        {line !== null && <span className="mt-0.5 block truncate text-[13px] leading-5 text-muted">{line}</span>}
        {stopped !== null && (
          // Why the system stopped it, in full.
          <span className="mt-0.5 flex items-start gap-1.5 text-[13px] leading-5 text-secondary">
            <CircleAlert size={13} strokeWidth={1.8} className="mt-[3px] shrink-0 text-warning" aria-hidden />
            <span>{stoppedLine(stopped, budget)}</span>
          </span>
        )}
      </span>
      <RecipientPill recipient={recipient} memberId={memberId} />
    </>
  );
  const row = "-mx-2 flex items-center gap-3 rounded-control px-2 py-2";
  return recipient.thread_id === null ? (
    <li className={row}>{body}</li>
  ) : (
    <li>
      <Link to={`/requests/${recipient.thread_id}`} className={`${row} transition-colors hover:bg-hover`}>
        {body}
      </Link>
    </li>
  );
}

/** "Sent to 2 · 1 not sent": rows that come only from the card don't count as sent. */
function sentMeta(recipients: readonly RecipientStatus[]): string {
  const sent = recipients.filter((recipient) => recipient.sent).length;
  const left = recipients.length - sent;
  return left === 0 ? `Sent to ${sent}` : `Sent to ${sent} · ${left} not sent`;
}

/** The live per-recipient status of one network request (UI_REFERENCE §4). */
export function RequestStatusPanel({ view, memberId }: { view: RequestStatusView; memberId: string | null }) {
  const titleId = useId();
  const budget = useBudgetState();
  return (
    <ToolCard
      labelledBy={titleId}
      icon={<Waypoints size={16} strokeWidth={1.6} />}
      title="Harmony Network Request"
      meta={sentMeta(view.recipients)}
      footer={
        <Link
          to="/requests"
          className="-ml-1 inline-flex items-center gap-0.5 rounded-md px-1 py-0.5 text-[13px] text-secondary transition-colors hover:text-ink"
        >
          Open in Requests
          <ChevronRight size={14} strokeWidth={1.8} />
        </Link>
      }
    >
      <p className="text-[15px] leading-6 font-medium text-ink">{view.title}</p>
      <ul className="mt-1.5">
        {view.recipients.map((recipient) => (
          <RecipientRow key={recipient.org.id} recipient={recipient} memberId={memberId} budget={budget} />
        ))}
      </ul>
      {view.screened !== null && <p className="mt-1 text-[12.5px] text-muted">{screenedLine(view.screened)}</p>}
    </ToolCard>
  );
}

export function RequestStatusCard({ requestId, memberId }: { requestId: string; memberId: string | null }) {
  const status = useRequestStatus(requestId);
  useRequestLive(requestId, status.data);
  if (status.data !== undefined) return <RequestStatusPanel view={status.data} memberId={memberId} />;
  return (
    <ToolCard
      icon={<Waypoints size={16} strokeWidth={1.6} />}
      title="Harmony Network Request"
      meta={status.isError ? "Couldn't load the status" : "Loading…"}
    />
  );
}
