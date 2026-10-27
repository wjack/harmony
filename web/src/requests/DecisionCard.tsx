/**
 * The decision card (LIVE_DEMO_PLAN 3.9): an open item, inline after the last transcript
 * row and read in full with it, never docked and never scrolled on its own. Whose it
 * is, the short question, why it came to the viewer (with Reassign), Claude's whole
 * draft (editable), what Claude looked at and its reasoning. The actions are the view's
 * one composer.
 */
import { ChevronLeft, ChevronRight, Eye, FileText, Lock, MessageCircleQuestion, Search, ShieldAlert, Wrench } from "lucide-react";
import { useId, useState, type KeyboardEvent, type ReactNode } from "react";
import { Link } from "react-router";

import { useHarmonySettings } from "../customize/api";
import { sharingPath } from "../customize/sections";
import { useCalendar } from "../demo/worldClock";
import { IconButton, Spark, cx } from "../shell/ui";
import { ReassignMenu } from "./ReassignMenu";
import { useAutoHeight } from "./autoHeight";
import { cardTitle, eyebrow, lookedAtSummary, routingOf, whyHeading } from "./decision";
import { longAge } from "./format";
import type { ActivityLine, EscalationDraft, MemberView, OrgRef } from "./types";
import type { Decision } from "./useDecision";

interface DecisionCardProps {
  decision: Decision;
  threadId: string;
  us: OrgRef;
  them: OrgRef;
  /** Our org's members, for Reassign. */
  members: readonly MemberView[];
  memberId: string | undefined;
}

export function DecisionCard({ decision, threadId, us, them, members, memberId }: DecisionCardProps) {
  const { escalation, consult, rev, drafts } = decision;
  const flaggedBySharing = consult?.needs_human === false && consult.sharing_check?.within_sharing === false;
  const settings = useHarmonySettings(us.slug).data;
  const routing = routingOf(escalation, memberId, us, settings && (settings.default_assignee?.id ?? null));
  const whyId = useId();

  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      decision.approve();
    } else if (event.key === "Escape" && decision.editing) {
      event.stopPropagation();
      decision.setEditing(false);
    }
  };

  return (
    <section
      aria-label="Needs your decision"
      onKeyDown={onKeyDown}
      className="rounded-composer border border-border-strong bg-surface shadow-card"
    >
      <header className="flex items-start gap-2 pt-4 pr-3 pb-1 pl-5">
        <div className="min-w-0 flex-1">
          <p className="flex items-start gap-1.5 text-[13px] leading-5 text-secondary">
            <MessageCircleQuestion size={14} strokeWidth={1.75} aria-hidden className="mt-[3px] shrink-0 text-warning" />
            <span>{eyebrow(escalation, them, memberId)}</span>
          </p>
          <h2 className="mt-1.5 text-[16px] leading-[1.45] font-medium break-words whitespace-pre-wrap text-ink">
            {cardTitle(escalation, rev, consult, them)}
          </h2>
        </div>
        {drafts.length > 1 && <RevPager index={decision.index} count={drafts.length} onChange={decision.showRev} />}
      </header>

      <div className="flex flex-col gap-4 px-5 pt-2 pb-4">
        <section aria-labelledby={whyId} className="flex flex-col gap-1 text-[14px] leading-[1.55]">
          <h3 id={whyId} className={LABEL}>
            {whyHeading(escalation, memberId)}
          </h3>
          {escalation.reason !== "" && (
            <p className="flex items-start gap-2 text-ink">
              {flaggedBySharing ? (
                <ShieldAlert size={14} strokeWidth={1.75} aria-hidden className="mt-[3px] shrink-0 text-warning" />
              ) : (
                <Lock size={14} strokeWidth={1.75} aria-hidden className="mt-[3px] shrink-0 text-warning" />
              )}
              <span>
                {escalation.reason}
                {flaggedBySharing && <span className="text-muted"> (the sharing check)</span>}
              </span>
            </p>
          )}
          <p className="flex flex-wrap items-baseline gap-x-1.5 pl-[22px] text-[13.5px] text-muted">
            <span>{routing.line}</span>
            {routing.with !== null && (
              <>
                <span aria-hidden>·</span>
                <span>{routing.with}</span>
              </>
            )}
            <span aria-hidden>·</span>
            <ReassignMenu
              escalation={escalation}
              threadId={threadId}
              memberId={memberId}
              us={us}
              members={members}
            />
          </p>
        </section>

        <Draft decision={decision} isConsult={escalation.kind === "consult"} />

        {rev !== null && rev.activity.length > 0 && <LookedAt rev={rev} />}

        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2">
          {/* A held message's rationale is its reason, already shown above. */}
          {rev !== null && rev.rationale !== "" && rev.rationale !== escalation.reason ? (
            <Reasoning text={rev.rationale} />
          ) : (
            <span />
          )}
          <Link
            to={sharingPath(them.id)}
            className="text-[12.5px] text-muted underline decoration-border-strong underline-offset-[3px] hover:text-ink hover:decoration-ink"
          >
            Edit what you share with {them.name}
          </Link>
        </div>
      </div>
    </section>
  );
}

function RevPager({ index, count, onChange }: { index: number; count: number; onChange: (index: number) => void }) {
  return (
    <div className="flex items-center gap-0.5 pt-0.5 text-[12.5px] text-muted tabular-nums">
      <IconButton
        label="Earlier draft"
        size="sm"
        disabled={index === 0}
        onClick={() => {
          onChange(index - 1);
        }}
      >
        <ChevronLeft size={15} strokeWidth={1.5} />
      </IconButton>
      <span aria-live="polite">
        {index + 1} of {count}
      </span>
      <IconButton
        label="Later draft"
        size="sm"
        disabled={index === count - 1}
        onClick={() => {
          onChange(index + 1);
        }}
      >
        <ChevronRight size={15} strokeWidth={1.5} />
      </IconButton>
    </div>
  );
}

/** A part's small label: "Why it came to you", "Draft reply". */
const LABEL = "text-[12.5px] font-medium text-secondary";

const DRAFT_TEXT = "font-serif text-[16px] leading-[1.6] break-words whitespace-pre-wrap text-ink";

/** "Draft reply": the whole draft as text, or a field that grows to fit it while editing. */
function Draft({ decision, isConsult }: { decision: Decision; isConsult: boolean }) {
  const calendar = useCalendar();
  const { rev, answer, editing, redrafting } = decision;
  return (
    <div className="border-t border-border pt-3.5">
      <div className="mb-1.5 flex items-center gap-1.5 text-[12.5px] text-muted">
        <Spark size={13} animated={redrafting} />
        <h3 className={LABEL}>{redrafting ? "Claude is redrafting…" : rev === null ? "Your answer" : "Draft reply"}</h3>
        {rev !== null && !redrafting && <span>· {longAge(rev.created_at, calendar)}</span>}
        {decision.edited && <span className="rounded-full bg-hover px-1.5 text-[11px] text-secondary">Edited</span>}
      </div>
      {editing ? (
        <GrowingField label={isConsult ? "Answer" : "Reply"} value={answer} onChange={decision.setAnswer} />
      ) : (
        // While Claude redrafts, the draft being replaced stays readable, dimmed.
        <p
          onDoubleClick={() => {
            decision.setEditing(true);
          }}
          aria-busy={redrafting}
          className={cx(DRAFT_TEXT, redrafting && "opacity-50")}
        >
          {answer === "" ? <span className="text-muted">No draft yet. Write the answer yourself.</span> : answer}
        </p>
      )}
    </div>
  );
}

/** A textarea as tall as its text, so the card never scrolls inside itself. */
function GrowingField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  const field = useAutoHeight(value);
  return (
    <textarea
      ref={field}
      aria-label={label}
      autoFocus
      rows={1}
      value={value}
      onChange={(event) => {
        onChange(event.target.value);
      }}
      className={cx(
        DRAFT_TEXT,
        "block w-full resize-none overflow-hidden rounded-control border border-border-strong bg-canvas px-3 py-2 outline-none focus:border-muted",
      )}
    />
  );
}

const TOOL_ICON: Readonly<Record<string, ReactNode>> = {
  search_org_knowledge: <Search size={12} strokeWidth={1.75} />,
  read_document: <FileText size={12} strokeWidth={1.75} />,
};

/** What internal Claude read to draft this: one line, opening to one row per tool call. */
function LookedAt({ rev }: { rev: EscalationDraft }) {
  const [open, setOpen] = useState(false);
  return (
    <section aria-label="What Claude looked at">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => {
          setOpen((value) => !value);
        }}
        className="flex w-full min-w-0 items-center gap-1.5 text-left text-[13px] leading-5 text-muted hover:text-ink"
      >
        <Eye size={14} strokeWidth={1.6} aria-hidden className="shrink-0" />
        <span className="min-w-0 truncate">Looked at: {lookedAtSummary(rev)}</span>
        <ChevronRight
          size={14}
          strokeWidth={1.5}
          aria-hidden
          className={cx("shrink-0 transition-transform", open && "rotate-90")}
        />
      </button>
      {open && <ActivityList lines={rev.activity} />}
    </section>
  );
}

function ActivityList({ lines }: { lines: readonly ActivityLine[] }) {
  return (
    <ul className="mt-2 flex flex-col gap-1 pl-5">
      {lines.map((line, index) => (
        // Lines have no id; a draft rev's activity never changes.
        <li key={`${String(index)}:${line.tool}`} className="flex items-start gap-2 text-[13px] leading-5">
          <span className="mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-[4px] bg-hover text-secondary">
            {TOOL_ICON[line.tool] ?? <Wrench size={12} strokeWidth={1.75} />}
          </span>
          <span className="min-w-0 flex-1 text-secondary">
            {line.label}
            {line.detail != null && line.detail !== "" && <span className="text-muted"> · {line.detail}</span>}
          </span>
        </li>
      ))}
    </ul>
  );
}

function Reasoning({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="min-w-0">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => {
          setOpen((value) => !value);
        }}
        className="flex items-center gap-1 text-[12.5px] text-muted hover:text-ink"
      >
        Claude's reasoning
        <ChevronRight size={13} strokeWidth={1.5} aria-hidden className={cx("transition-transform", open && "rotate-90")} />
      </button>
      {open && <p className="mt-1.5 text-[13px] leading-[1.6] text-secondary">{text}</p>}
    </div>
  );
}
