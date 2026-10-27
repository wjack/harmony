/**
 * The quiet rows only our side sees (composer.html, EDIT.md §8.3): an Edit and how it
 * went, a decision on an open item, an answer Claude released on its own, and the
 * steady last row that says whose move it is. Blue marks what went to our own Claude.
 */
import { CircleCheck, CircleX, Clock, Loader2, Sparkles, UserRound } from "lucide-react";
import type { ReactNode } from "react";

import type { ReopenOffer } from "../api/types";
import type { BudgetState } from "../app/budget";
import { firstName } from "../shell/names";
import { cx } from "../shell/ui";
import { EventRow, LinkButton, Said, When, Who } from "./EventRow";
import { steerOutcome } from "./failure";
import { onlyUs } from "./format";
import type { ReleaseEntry, ResolutionEntry, SteerEntry } from "./timeline";
import type { OrgRef, Side } from "./types";
import type { Reopen } from "./useReopen";

/** What the rows can do beyond showing themselves, from the view. */
export interface RowActions {
  side: Side;
  budget: BudgetState;
  /** Our Claude follows guidance on this thread. */
  hasGuidance: boolean;
  onSeeGuidance: () => void;
  /** Puts an Edit's text back in the box's Edit. */
  onRetryEdit: (text: string) => void;
  reopen: Reopen;
  /** Whether our side may reopen the thread now (`ThreadDetail.reopen`). */
  reopenOffer: ReopenOffer | null;
}

const ICON = { size: 15, strokeWidth: 1.75 } as const;

export function SteerRow({ entry, us, actions }: { entry: SteerEntry; us: OrgRef; actions: RowActions }) {
  const { steer } = entry;
  const who = firstName(steer.author.name);
  const outcome = steerOutcome(steer, actions.budget, actions.hasGuidance);
  return (
    <EventRow
      icon={<Sparkles {...ICON} className="text-link" />}
      label={`${who} told Claude what to do differently`}
      header={
        <>
          <Who>{who}</Who> told Claude what to do differently · {onlyUs(us)} · <When at={entry.at} />
        </>
      }
    >
      <Said>{steer.body}</Said>
      <p
        className={cx(
          outcome.tone === "working" && "inline-flex items-center gap-1.5 text-link",
          outcome.tone === "failed" && "text-warning",
        )}
      >
        {outcome.tone === "working" && <Loader2 size={13} strokeWidth={2} aria-hidden className="animate-spin" />}
        {outcome.text}
        {outcome.action === "see_guidance" && (
          <>
            {" "}
            <LinkButton onClick={actions.onSeeGuidance}>See guidance</LinkButton>
          </>
        )}
        {outcome.action === "try_again" && (
          <>
            {" "}
            <LinkButton
              onClick={() => {
                actions.onRetryEdit(steer.body);
              }}
            >
              Try again
            </LinkButton>
          </>
        )}
      </p>
    </EventRow>
  );
}

function resolutionSentence(entry: ResolutionEntry, them: OrgRef): string {
  const { kind, resolution } = entry.escalation;
  const consult = kind === "consult";
  switch (resolution?.action) {
    case "approve":
      return consult ? "approved the answer" : "approved Claude's message";
    case "decline":
      return consult ? "declined to answer" : "declined Claude's message";
    case "take_over":
      return `replied to ${them.name} directly`;
    default:
      return "resolved the item";
  }
}

function resolutionIcon(entry: ResolutionEntry): ReactNode {
  switch (entry.escalation.resolution?.action) {
    case "decline":
      return <CircleX {...ICON} />;
    case "take_over":
      return <UserRound {...ICON} />;
    default:
      return <CircleCheck {...ICON} />;
  }
}

/** A teammate decided an open item. A decline quotes the reason, which Claude may pass on. */
export function ResolutionRow({ entry, us, them }: { entry: ResolutionEntry; us: OrgRef; them: OrgRef }) {
  const who = entry.by === null ? "A teammate" : firstName(entry.by.name);
  const sentence = resolutionSentence(entry, them);
  const resolution = entry.escalation.resolution;
  const reason = resolution?.action === "decline" ? (resolution.text ?? "") : "";
  return (
    <EventRow
      icon={resolutionIcon(entry)}
      label={`${who} ${sentence}`}
      header={
        <>
          <Who>{who}</Who> {sentence} · {onlyUs(us)} · <When at={entry.at} />
        </>
      }
    >
      {reason !== "" && (
        <>
          <Said>{reason}</Said>
          <p>Claude may pass this reason on to {them.name}.</p>
        </>
      )}
    </EventRow>
  );
}

/** An answer that went back to our Claude for this thread: approved by a teammate, or released without one. */
export function ReleaseRow({ entry, us }: { entry: ReleaseEntry; us: OrgRef }) {
  const who = entry.by === null ? "Claude" : firstName(entry.by.name);
  const sentence = entry.by === null ? "answered without a teammate" : "approved the answer";
  return (
    <EventRow
      icon={<CircleCheck {...ICON} />}
      label={`${who} ${sentence}`}
      header={
        <>
          <Who>{who}</Who> {sentence} · {onlyUs(us)} · <When at={entry.at} />
        </>
      }
    />
  );
}

/** The steady last row while nothing of ours is being written: whose move it is. */
export function WaitingLine({ text }: { text: string }) {
  return (
    <p role="status" className="flex gap-3 text-ui-sm text-muted">
      <span aria-hidden className="flex h-[18px] w-7 shrink-0 items-center justify-center">
        <Clock {...ICON} />
      </span>
      <span className="min-w-0 flex-1">{text}</span>
    </p>
  );
}
