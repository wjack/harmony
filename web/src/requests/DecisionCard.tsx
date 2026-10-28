/**
 * The decision card (EDIT.md §8.3, composer.html): an open item, inline after the last
 * transcript row and read in full with it, never scrolled on its own. Whose it is (with
 * Reassign), what it asks and why it waits, or that Claude declined a step, with Retry;
 * Claude's whole draft, edited where it stands; what Claude looked at; and a foot that
 * sticks while the card is taller than the view, with only Decline and Approve and send.
 * Telling Claude what to change is the box's Edit, below.
 */
import { ChevronRight, CircleHelp, Eye, FileText, Pencil, RotateCcw, Search, Sparkles, Wrench } from "lucide-react";
import { useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";

import { CardFoot, CardHint, DeclineStep, EditableText, PartLabel, useApproveShortcut } from "../cards";
import { BudgetStrip } from "../chat/BudgetStrip";
import { useHarmonySettings } from "../customize/api";
import { MODE_INFO } from "../customize/modes";
import { sharingPath } from "../customize/sections";
import { Button, cx } from "../shell/ui";
import { DraftedFrom } from "./EventRow";
import { ReassignMenu } from "./ReassignMenu";
import {
  ANSWER_MAX_CHARS,
  AUTONOMY_PATH,
  approveHint,
  cardTitle,
  declineCopy,
  draftedFrom,
  draftLabel,
  eyebrow,
  lookedAtSummary,
  modeLine,
  reasonLine,
  refusalLine,
  sharingLabel,
} from "./decision";
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
  /** The viewer can change what our side shares; anyone else reads it. */
  admin: boolean;
  /** The box below holds an instruction to Claude that isn't sent yet: the draft it will rewrite is marked. */
  unsentEdit: boolean;
}

export function DecisionCard({ decision, threadId, us, them, members, memberId, admin, unsentEdit }: DecisionCardProps) {
  const { escalation, consult, rev, rewriting, retrying, blocked } = decision;
  const card = useRef<HTMLElement>(null);
  const [declining, setDeclining] = useState(false);
  const held = escalation.kind === "outbound_review";
  const reason = reasonLine(escalation);
  const drafted = draftedFrom(consult);
  useApproveShortcut(card, decision.approve, !declining && !rewriting && !retrying && blocked === null);

  // A held message waits because of how our side works with them: say which mode, and where to change it.
  const settings = useHarmonySettings(us.slug).data;
  const autonomy =
    settings?.trusted_organizations.find((row) => row.counterparty.id === them.id)?.autonomy ?? settings?.autonomy;

  let hintRight: ReactNode = (
    <>
      {approveHint(escalation, them, decision.edited)}
      <span className="text-faint"> · ⌘↵</span>
    </>
  );
  if (blocked !== null) hintRight = "Approving and declining wait until the budget is back";
  else if (rewriting || retrying) hintRight = "You can approve once the new draft is here";

  let hintLeft: ReactNode;
  if (held && autonomy !== undefined) {
    hintLeft = (
      <span>
        {modeLine(MODE_INFO[autonomy].label, them)} ·{" "}
        <Link to={AUTONOMY_PATH} className="text-link hover:underline">
          Change
        </Link>
      </span>
    );
  }

  const decline = declineCopy(escalation, them);

  return (
    <div className="relative">
      {blocked !== null && <BudgetStrip line={blocked} />}
      <section
        ref={card}
        aria-label="Needs your decision"
        className="relative rounded-composer border border-border-strong bg-surface shadow-card"
      >
        <div className="flex flex-col gap-1.5 px-4 pt-3.5 pb-4">
          <div className="flex items-start gap-3">
            <p className="flex min-w-0 flex-1 items-start gap-1.5 text-ui-sm text-muted">
              <CircleHelp size={14} strokeWidth={1.75} aria-hidden className="mt-0.5 shrink-0" />
              <span>{eyebrow(escalation, them, memberId)}</span>
            </p>
            <ReassignMenu escalation={escalation} threadId={threadId} memberId={memberId} us={us} members={members} />
          </div>
          <h2 className="text-ui-title font-medium break-words whitespace-pre-wrap text-ink">
            {cardTitle(escalation, rev, consult, them)}
          </h2>
          {decision.refusal !== null || retrying ? (
            <RefusalNote decision={decision} />
          ) : (
            reason !== null && <CardHint>{reason}</CardHint>
          )}

          <div className="mt-2 border-t border-border pt-3">
            <DraftLabel decision={decision} label={draftLabel(escalation)} unsentEdit={unsentEdit} />
            <Draft decision={decision} label={draftLabel(escalation)} unsentEdit={unsentEdit} />
          </div>

          {rev !== null && (rev.activity.length > 0 || drafted !== null) && <LookedAt rev={rev} drafted={drafted} />}
          <Link
            to={sharingPath(them.id)}
            className="self-start text-ui-xs text-muted underline decoration-border-strong underline-offset-[3px] hover:text-ink hover:decoration-ink"
          >
            {sharingLabel(admin, them)}
          </Link>
          {decision.notice !== null && (
            <p
              role={decision.notice.tone === "error" ? "alert" : "status"}
              className={cx(
                "mt-1 rounded-control px-3 py-2 text-ui-sm",
                decision.notice.tone === "error" ? "bg-danger-soft text-danger" : "bg-info-soft text-link",
              )}
            >
              {decision.notice.text}
            </p>
          )}
        </div>

        {declining ? (
          <DeclineStep
            required
            title={decline.title}
            caption={decline.caption}
            pending={decision.busy}
            onCancel={() => {
              setDeclining(false);
            }}
            onConfirm={(text) => {
              decision.decline(text, () => {
                setDeclining(false);
              });
            }}
          />
        ) : (
          <CardFoot
            onDecline={() => {
              decision.clearNotice();
              setDeclining(true);
            }}
            onApprove={decision.approve}
            approveDisabled={decision.busy || rewriting || retrying || blocked !== null}
            declineDisabled={decision.busy || blocked !== null}
            hintLeft={hintLeft}
            hintRight={hintRight}
          />
        )}
      </section>
    </div>
  );
}

/**
 * In the reason's place: Claude declined the step the item waits on (drafting the answer,
 * or checking the draft against what we share), with Retry. The draft above the foot is
 * where the member edits what goes in: an answer Claude declined to draft can be written
 * and approved, and one it declined to check is checked as the member left it.
 */
function RefusalNote({ decision }: { decision: Decision }) {
  const { refusal, retrying, busy, blocked, edited } = decision;
  if (retrying || refusal === null) {
    return (
      <p role="status" className="flex items-center gap-1.5 text-ui-sm text-link">
        <Sparkles size={13} strokeWidth={1.75} aria-hidden />
        Claude is trying again…
      </p>
    );
  }
  const withEdits = refusal.step === "sharing_check" && edited;
  return (
    <div className="flex flex-wrap items-center gap-x-1 gap-y-1">
      <CardHint>{refusalLine(refusal)}</CardHint>
      <Button
        variant="ghost"
        size="sm"
        icon={<RotateCcw size={13} strokeWidth={1.75} />}
        disabled={busy || blocked !== null}
        title={blocked ?? undefined}
        onClick={decision.retry}
      >
        {withEdits ? "Retry with your edits" : "Retry"}
      </Button>
    </div>
  );
}

/**
 * The draft part's label row: what the draft is and where it came from (rewritten by
 * Claude, edited by you, unchanged), or that Claude is rewriting it, or that an
 * unsent Edit will; on the right, the controls, else how to change the wording.
 */
function DraftLabel({ decision, label, unsentEdit }: { decision: Decision; label: string; unsentEdit: boolean }) {
  const { drafts, index, rewriting, edited, unchanged } = decision;
  if (rewriting) {
    return (
      <p className="flex items-center gap-1.5 text-ui-sm text-link">
        <Sparkles size={13} strokeWidth={1.75} aria-hidden />
        Claude is rewriting…
      </p>
    );
  }
  const pager = drafts.length > 1;
  let right: ReactNode = null;
  if (unsentEdit) {
    right = (
      <span className="inline-flex items-center gap-1 text-link">
        <Sparkles size={13} strokeWidth={1.75} aria-hidden />
        Claude rewrites this when you tell it
      </span>
    );
  } else if (!edited && !pager) {
    right = (
      <span className="inline-flex items-center gap-1 text-faint">
        <Pencil size={12} strokeWidth={1.75} aria-hidden />
        Click the text to change the wording
      </span>
    );
  }
  return (
    <div className="flex items-center gap-3 text-ui-sm">
      <div className="min-w-0 flex-1">
        <PartLabel
          label={unchanged ? `${label} · unchanged` : label}
          rewritten={
            pager
              ? {
                  index,
                  count: drafts.length,
                  onPrev: () => {
                    decision.showRev(index - 1);
                  },
                  onNext: () => {
                    decision.showRev(index + 1);
                  },
                }
              : undefined
          }
          edited={edited ? { onUndo: decision.undoEdits } : undefined}
        />
      </div>
      {right !== null && <span className="shrink-0">{right}</span>}
    </div>
  );
}

/**
 * The whole draft, edited where it stands. While an unsent Edit will rewrite it, it's
 * marked in blue; while Claude rewrites it, it stays readable, dimmed and locked.
 */
function Draft({ decision, label, unsentEdit }: { decision: Decision; label: string; unsentEdit: boolean }) {
  const { rev, answer, rewriting } = decision;
  return (
    <div
      aria-busy={rewriting}
      className={cx(
        "mt-1.5 rounded-control",
        rewriting && "opacity-50",
        unsentEdit && !rewriting && "bg-info-soft/50 px-2 shadow-[inset_2px_0_0_var(--info)]",
      )}
    >
      <EditableText
        value={answer}
        original={rev?.answer ?? ""}
        onChange={decision.setAnswer}
        label={label}
        disabled={rewriting}
        maxLength={ANSWER_MAX_CHARS}
      />
      {rev === null && answer === "" && <p className="text-ui-sm text-muted">No draft yet. Write the answer yourself.</p>}
    </div>
  );
}

const TOOL_ICON: Readonly<Record<string, ReactNode>> = {
  search_org_knowledge: <Search size={12} strokeWidth={1.75} />,
  read_document: <FileText size={12} strokeWidth={1.75} />,
};

/**
 * What internal Claude read to draft this: one line, opening to one row per tool call,
 * led by the member's earlier answer when the draft started from one.
 */
function LookedAt({ rev, drafted }: { rev: EscalationDraft; drafted: string | null }) {
  const [open, setOpen] = useState(false);
  return (
    <section aria-label="What Claude looked at" className="mt-1 flex flex-col gap-1">
      {drafted !== null && <DraftedFrom line={drafted} />}
      {rev.activity.length > 0 && (
        <button
          type="button"
          aria-expanded={open}
          onClick={() => {
            setOpen((value) => !value);
          }}
          className="flex w-full min-w-0 items-center gap-1.5 text-left text-ui-sm text-muted hover:text-ink"
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
      )}
      {open && <ActivityList lines={rev.activity} />}
    </section>
  );
}

function ActivityList({ lines }: { lines: readonly ActivityLine[] }) {
  return (
    <ul className="mt-1 flex flex-col gap-1 pl-5">
      {lines.map((line, index) => (
        // Lines have no id; a draft rev's activity never changes.
        <li key={`${String(index)}:${line.tool}`} className="flex items-start gap-2 text-ui-sm">
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
