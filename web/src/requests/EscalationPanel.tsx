/**
 * The escalation panel: an inbox item waiting on a person (UI_REFERENCE §4, §7;
 * REVISION_UNSTRUCTURED §13). The decision is a card at the end of the transcript,
 * read in full with the thread: the question, why it needs them (with Reassign),
 * Claude's whole draft and what internal Claude looked at. The actions sit in a
 * composer at the bottom of the pane (`actionsSlot`), in reach wherever the reader has
 * scrolled: approve and send, edit, ask Claude to redraft, decline.
 *
 * An approval releases the answer to this one thread. Sharing more from now on is a
 * change to what the org shares, so the panel links there.
 */
import {
  ChevronLeft,
  ChevronRight,
  FileText,
  Lock,
  MessageCircleQuestion,
  Search,
  ShieldAlert,
  WandSparkles,
  Wrench,
  X,
} from "lucide-react";
import { useEffect, useState, type KeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router";

import { sharingPath } from "../customize/sections";
import { ScriptedNote } from "../demo/ScriptedNote";
import { useCalendar } from "../demo/worldClock";
import { toast } from "../shell/toasts";
import { Button, IconButton, Spark, cx } from "../shell/ui";
import { ReassignMenu, RoutedText } from "./RoutingLine";
import { problemOf, useRedraftEscalation, useResolveEscalation } from "./api";
import { approveBody, declineBody, isEdited, shownAnswer, sortedDrafts, type DraftEdits } from "./draft";
import { longAge, representativeName } from "./format";
import { useFailedRedrafts } from "./live";
import type { ActivityLine, ConsultView, EscalationView, MemberView, OrgRef, ResolveEscalation } from "./types";

/** What the footer's input is for: a redraft instruction, a decline reason, or nothing (an outbound review). */
type Mode = "redraft" | "decline" | null;

/**
 * How long a redraft may take before the panel stops waiting and lets the member retry.
 * A redraft that fails says so at once; this covers a report the stream dropped.
 */
export const REDRAFT_TIMEOUT_MS = 60_000;

interface Notice {
  tone: "error" | "info";
  text: string;
  action?: { label: string; run: () => void };
}

export interface EscalationPanelProps {
  escalation: EscalationView;
  consult: ConsultView | null;
  threadId: string;
  us: OrgRef;
  them: OrgRef;
  /** Our org's members, for Reassign. */
  members: readonly MemberView[];
  memberId: string | undefined;
  reassignOpen: boolean;
  onReassignOpen: (open: boolean) => void;
  /** Where the actions render: the pane's bottom edge, outside the transcript's scroll. */
  actionsSlot: HTMLElement | null;
}

function heading(escalation: EscalationView, consult: ConsultView | null, us: OrgRef, them: OrgRef) {
  if (escalation.kind === "consult") {
    return {
      eyebrow: `${representativeName(us.name)} is asking the team`,
      title: consult?.question ?? `${representativeName(us.name)} needs an answer before it replies`,
    };
  }
  return { eyebrow: `Before it goes to ${them.name}`, title: `Review Claude's reply to ${them.name}` };
}

function successToast(body: ResolveEscalation, escalation: EscalationView, us: OrgRef, them: OrgRef): string {
  const consult = escalation.kind === "consult";
  if (body.action === "approve") return consult ? `Released to ${representativeName(us.name)}` : `Sent to ${them.name}`;
  return consult ? `Declined. Claude will let ${them.name} know.` : "Declined. The reply won't be sent.";
}

export function EscalationPanel({
  escalation,
  consult,
  threadId,
  us,
  them,
  members,
  memberId,
  reassignOpen,
  onReassignOpen,
  actionsSlot,
}: EscalationPanelProps) {
  const target = { threadId, escalationId: escalation.id };
  const resolve = useResolveEscalation(target);
  const redraft = useRedraftEscalation(target);

  const isConsult = escalation.kind === "consult";
  const drafts = sortedDrafts(escalation.drafts);
  const [revIndex, setRevIndex] = useState<number | null>(null);
  const rev = drafts[revIndex ?? drafts.length - 1] ?? null;

  const [editsByRev, setEditsByRev] = useState<Record<number, DraftEdits>>({});
  const edits = editsByRev[rev?.draft_rev ?? 0] ?? {};
  const [editing, setEditing] = useState(false);
  const idle: Mode = isConsult ? "redraft" : null;
  const [mode, setMode] = useState<Mode>(idle);
  const [text, setText] = useState("");
  const [notice, setNotice] = useState<Notice | null>(null);
  const [awaiting, setAwaiting] = useState<{ rev: number; instruction: string } | null>(null);

  // A redraft lands as a new rev. Until it does, the shown draft is marked as being
  // redrafted and can't be approved; a redraft that fails, or never lands, stops the wait.
  const newestRev = drafts.at(-1)?.draft_rev ?? null;
  const failedRevs = useFailedRedrafts(escalation.id);
  const failed = awaiting !== null && failedRevs.has(awaiting.rev) ? awaiting : null;
  const redrafting = awaiting !== null && failed === null && (newestRev === null || newestRev < awaiting.rev);
  const retry = (instruction: string) => ({
    label: "Try again",
    run: () => {
      setAwaiting(null);
      setNotice(null);
      setMode("redraft");
      setText(instruction);
    },
  });
  useEffect(() => {
    if (!redrafting) return;
    const timer = window.setTimeout(() => {
      setAwaiting(null);
      setNotice({ tone: "error", text: "Claude didn't come back with a new draft.", action: retry(awaiting.instruction) });
    }, REDRAFT_TIMEOUT_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [redrafting, awaiting]);
  const shownNotice: Notice | null =
    failed === null
      ? notice
      : {
          tone: "error",
          text: "Claude couldn't redraft this. Edit the draft yourself, or try again.",
          action: retry(failed.instruction),
        };

  const busy = resolve.isPending || redraft.isPending;
  const approveBlocked = busy || redrafting;
  const canEdit = !redrafting;
  const answer = shownAnswer(rev, edits);
  const { eyebrow, title } = heading(escalation, consult, us, them);
  const flaggedBySharing = consult?.needs_human === false && consult.sharing_check?.within_sharing === false;

  const setAnswer = (value: string) => {
    const key = rev?.draft_rev ?? 0;
    setEditsByRev((current) => ({ ...current, [key]: { answer: value } }));
  };

  const reset = () => {
    setMode(idle);
    setText("");
  };

  const fail = (error: unknown) => {
    const problem = problemOf(error);
    if (problem?.code === "escalation_not_open") {
      // `resolved_by` is set only when someone resolved it; a redraft already running
      // or a cancelled item comes back without it.
      setNotice(
        problem.resolved_by == null
          ? { tone: "info", text: "This item changed while you were deciding. Check the latest before trying again." }
          : { tone: "info", text: `Already resolved by ${problem.resolved_by.name}.` },
      );
      return;
    }
    setNotice({ tone: "error", text: problem?.detail ?? (error instanceof Error ? error.message : "Something went wrong.") });
  };

  const send = (body: ResolveEscalation) => {
    setNotice(null);
    resolve.mutate(body, {
      onSuccess: () => {
        toast(successToast(body, escalation, us, them));
        setEditing(false);
        reset();
      },
      onError: fail,
    });
  };

  const approve = () => {
    if (approveBlocked) return;
    const built = approveBody(escalation.kind, rev, edits);
    if (!built.ok) {
      setNotice({ tone: "error", text: built.error });
      return;
    }
    send(built.body);
  };

  const decline = () => {
    if (busy) return;
    send(declineBody(text, rev));
  };

  const requestRedraft = () => {
    const instruction = text.trim();
    if (instruction === "" || redrafting || busy) return;
    setNotice(null);
    redraft.mutate(instruction, {
      onSuccess: (started) => {
        setAwaiting({ rev: started.draft_rev, instruction });
        setRevIndex(null);
        setEditing(false);
        reset();
      },
      onError: (error) => {
        const problem = problemOf(error);
        if (problem?.code === "escalation_not_open" && problem.resolved_by == null) {
          setNotice({ tone: "info", text: "Claude is still working on an earlier redraft. Try again once it lands." });
          return;
        }
        fail(error);
      },
    });
  };

  const typed = text.trim() !== "";
  const submit = () => {
    if (mode === "decline") decline();
    else if (mode === "redraft" && typed) requestRedraft();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      if (mode === "decline" || (mode === "redraft" && typed)) submit();
      else approve();
    } else if (event.key === "Escape" && (editing || mode === "decline" || typed)) {
      event.stopPropagation();
      setEditing(false);
      reset();
    }
  };

  const hint = mode === "decline" ? "decline" : mode === "redraft" && typed ? "redraft" : "approve";

  const actions = (
    <div onKeyDown={onKeyDown} className="mx-auto w-full max-w-[760px]">
      <section
        aria-label="Your decision"
        className="rounded-composer border border-border bg-surface shadow-composer"
      >
        {shownNotice !== null && (
          <div
            role={shownNotice.tone === "error" ? "alert" : "status"}
            className={cx(
              "mx-3 mt-3 flex items-center gap-3 rounded-control px-3 py-2 text-[13px] leading-5",
              shownNotice.tone === "error" ? "bg-danger-soft text-danger" : "bg-info-soft text-info",
            )}
          >
            <span className="flex-1">{shownNotice.text}</span>
            {shownNotice.action !== undefined && (
              <Button size="sm" variant="secondary" onClick={shownNotice.action.run}>
                {shownNotice.action.label}
              </Button>
            )}
          </div>
        )}
        <Footer
          mode={mode}
          text={text}
          them={them}
          busy={busy}
          approveBlocked={approveBlocked}
          redrafting={redrafting}
          editing={editing}
          canEdit={canEdit}
          onText={setText}
          onSubmit={submit}
          onCancel={() => {
            reset();
            setNotice(null);
          }}
          onApprove={approve}
          onDecline={() => {
            setNotice(null);
            setText("");
            setMode("decline");
          }}
          onToggleEdit={() => {
            setEditing((value) => !value);
          }}
        />
      </section>
      <p className="mt-2 text-center text-[11.5px] text-faint">
        {isEdited(edits) ? "Edited · " : ""}⌘↵ to {hint} · Esc to cancel
      </p>
      <ScriptedNote className="mt-1 text-center" />
    </div>
  );

  return (
    <>
      <section
        aria-label="Needs your decision"
        onKeyDown={onKeyDown}
        className="rounded-composer border border-border bg-surface shadow-composer"
      >
        <header className="flex items-start gap-2 pt-4 pr-3 pb-1 pl-5">
          <div className="min-w-0 flex-1">
            <p className="flex items-center gap-1.5 text-[12px] text-muted">
              <MessageCircleQuestion size={13} strokeWidth={1.75} aria-hidden />
              <span className="truncate">{eyebrow}</span>
            </p>
            <h2 className="mt-1 font-serif text-[17px] leading-[1.45] break-words whitespace-pre-wrap text-ink">
              {title}
            </h2>
            <div className="mt-1 flex items-start gap-3 text-[12.5px] leading-[1.5] text-muted">
              <p className="min-w-0 flex-1">
                <Spark size={12} className="mr-1.5 inline-block align-[-1px]" />
                <RoutedText escalation={escalation} memberId={memberId} />
              </p>
              <span className="shrink-0">
                <ReassignMenu
                  escalation={escalation}
                  threadId={threadId}
                  memberId={memberId}
                  us={us}
                  members={members}
                  open={reassignOpen}
                  onOpenChange={onReassignOpen}
                />
              </span>
            </div>
          </div>
          {drafts.length > 1 && (
            <RevPager
              index={revIndex ?? drafts.length - 1}
              count={drafts.length}
              onChange={(index) => {
                setRevIndex(index === drafts.length - 1 ? null : index);
                setEditing(false);
              }}
            />
          )}
        </header>

        <div className="flex flex-col gap-4 px-5 pt-3 pb-4">
          {escalation.reason !== "" && (
            <div className="flex items-start gap-2 rounded-control bg-warning-soft px-3 py-2 text-[13px] leading-5 text-secondary">
              {flaggedBySharing ? (
                <ShieldAlert size={13} strokeWidth={1.75} aria-hidden className="mt-[3px] shrink-0 text-warning" />
              ) : (
                <Lock size={13} strokeWidth={1.75} aria-hidden className="mt-[3px] shrink-0 text-warning" />
              )}
              <p>
                <span className="font-medium text-ink">Why this needs you: </span>
                {escalation.reason}
                {flaggedBySharing && <span className="text-muted"> (the sharing check)</span>}
              </p>
            </div>
          )}

          <AnswerBlock
            label={isConsult ? "Claude's draft" : "Claude's reply"}
            answer={answer}
            editing={editing && canEdit}
            edited={isEdited(edits)}
            draftedAt={rev?.created_at ?? null}
            redrafting={redrafting}
            onChange={setAnswer}
            onEdit={() => {
              if (canEdit) setEditing(true);
            }}
          />

          {rev !== null && rev.activity.length > 0 && <LookedAt lines={rev.activity} />}
          {/* A held message's rationale is its reason, already shown above. */}
          {rev !== null && rev.rationale !== "" && rev.rationale !== escalation.reason && (
            <Reasoning text={rev.rationale} />
          )}
        </div>

        <p className="flex flex-wrap items-baseline gap-x-1.5 border-t border-border px-5 py-2.5 text-[12.5px] leading-5 text-muted">
          <span>
            {isConsult
              ? "Approving releases this answer to this thread only."
              : `Approving sends this reply to ${them.name}.`}
          </span>
          <Link
            to={sharingPath(them.id)}
            className="font-medium text-ink underline decoration-border-strong underline-offset-[3px] hover:decoration-ink"
          >
            Edit what you share with {them.name}
          </Link>
        </p>
      </section>
      {actionsSlot !== null && createPortal(actions, actionsSlot)}
    </>
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

interface AnswerBlockProps {
  label: string;
  answer: string;
  editing: boolean;
  edited: boolean;
  draftedAt: string | null;
  redrafting: boolean;
  onChange: (value: string) => void;
  onEdit: () => void;
}

function AnswerBlock({ label, answer, editing, edited, draftedAt, redrafting, onChange, onEdit }: AnswerBlockProps) {
  const calendar = useCalendar();
  return (
    <div className="rounded-[14px] border border-border bg-canvas px-4 pt-3 pb-3.5">
      <div className="mb-1.5 flex items-center gap-1.5">
        <Spark size={13} animated={redrafting} />
        <span className="text-[12.5px] font-medium text-secondary">
          {redrafting ? "Claude is redrafting…" : draftedAt === null ? "Your answer" : label}
        </span>
        {draftedAt !== null && !redrafting && <span className="text-[12px] text-muted">· {longAge(draftedAt, calendar)}</span>}
        {edited && <span className="rounded-full bg-hover px-1.5 text-[11px] text-secondary">Edited</span>}
      </div>
      {editing ? (
        <textarea
          aria-label="Answer"
          autoFocus
          value={answer}
          onChange={(event) => {
            onChange(event.target.value);
          }}
          rows={Math.min(12, Math.max(4, Math.ceil(answer.length / 80) + 1))}
          className="w-full resize-y rounded-control border border-border-strong bg-surface px-3 py-2 font-serif text-[15px] leading-[1.6] text-ink outline-none focus:border-muted"
        />
      ) : (
        // While Claude redrafts, the draft being replaced stays readable, dimmed.
        <p
          onDoubleClick={onEdit}
          aria-busy={redrafting}
          className={cx(
            "font-serif text-[15px] leading-[1.6] break-words whitespace-pre-wrap text-ink",
            redrafting && "opacity-50",
          )}
        >
          {answer === "" ? <span className="text-muted">No draft yet. Write the answer yourself.</span> : answer}
        </p>
      )}
    </div>
  );
}

const TOOL_ICON: Readonly<Record<string, ReactNode>> = {
  search_org_knowledge: <Search size={12} strokeWidth={1.75} />,
  read_document: <FileText size={12} strokeWidth={1.75} />,
};

/** What internal Claude read to draft this, one line per tool call. */
function LookedAt({ lines }: { lines: readonly ActivityLine[] }) {
  return (
    <section aria-label="What Claude looked at">
      <h3 className="mb-1.5 text-[12.5px] text-muted">What Claude looked at</h3>
      <ul className="flex flex-col gap-1">
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
    </section>
  );
}

function Reasoning({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
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

interface FooterProps {
  mode: Mode;
  text: string;
  them: OrgRef;
  busy: boolean;
  /** Approve can't run: busy, or the shown draft is being redrafted. */
  approveBlocked: boolean;
  redrafting: boolean;
  editing: boolean;
  canEdit: boolean;
  onText: (value: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
  onApprove: () => void;
  onDecline: () => void;
  onToggleEdit: () => void;
}

/**
 * The card's bottom row. For a consult its input asks Claude to redraft; typing turns
 * the buttons into Redraft. Decline swaps the input for the reason Claude can give.
 */
function Footer({
  mode,
  text,
  them,
  busy,
  approveBlocked,
  redrafting,
  editing,
  canEdit,
  onText,
  onSubmit,
  onCancel,
  onApprove,
  onDecline,
  onToggleEdit,
}: FooterProps) {
  const typed = text.trim() !== "";
  let actions: ReactNode;
  if (mode === "decline") {
    actions = (
      <>
        <Button size="sm" variant="ghost" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
        <Button size="sm" variant="danger" disabled={busy} onClick={onSubmit}>
          Decline
        </Button>
      </>
    );
  } else if (mode === "redraft" && typed) {
    actions = (
      <Button size="sm" variant="primary" disabled={busy || redrafting} onClick={onSubmit}>
        Redraft
      </Button>
    );
  } else {
    actions = (
      <>
        <Button size="sm" variant="secondary" disabled={busy} onClick={onDecline}>
          Decline
        </Button>
        {canEdit && (
          <Button size="sm" variant="secondary" disabled={busy} onClick={onToggleEdit}>
            {editing ? "Done" : "Edit"}
          </Button>
        )}
        <Button size="sm" variant="primary" disabled={approveBlocked} onClick={onApprove}>
          Approve & send
        </Button>
      </>
    );
  }

  const placeholder =
    mode === "decline" ? `The reason Claude can give ${them.name} (optional)…` : "Tell Claude what to change…";

  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 py-2.5 pr-2.5 pl-3">
      {mode === null ? (
        <span className="flex-1" />
      ) : (
        <div className="flex min-w-[220px] flex-1 items-center gap-2.5">
          {mode === "decline" ? (
            <span className="flex size-7 shrink-0 items-center justify-center rounded-control bg-danger-soft text-danger">
              <X size={14} strokeWidth={1.75} aria-hidden />
            </span>
          ) : (
            <span className="flex size-7 shrink-0 items-center justify-center rounded-control border border-border bg-canvas text-secondary">
              <WandSparkles size={13} strokeWidth={1.6} aria-hidden />
            </span>
          )}
          <input
            key={mode}
            autoFocus={mode === "decline"}
            aria-label={mode === "decline" ? "Reason for declining" : "Redraft instruction"}
            value={text}
            disabled={mode === "redraft" && redrafting}
            placeholder={placeholder}
            onChange={(event) => {
              onText(event.target.value);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.metaKey && !event.ctrlKey) {
                event.preventDefault();
                onSubmit();
              }
            }}
            className="h-8 min-w-0 flex-1 bg-transparent text-[14px] text-ink outline-none placeholder:text-muted disabled:cursor-default"
          />
        </div>
      )}
      <div className="ml-auto flex items-center gap-2">{actions}</div>
    </div>
  );
}
