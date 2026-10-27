/**
 * The Harmony Network Request card, inline at the end of the chat (FEEDBACK_R2_PLAN
 * §2.5, `outreach.html`). While it waits on the member it is the request view's
 * decision card in the chat's words: who gets it, the brief and each ask editable where
 * they stand, Decline and Approve and send. Once decided it folds to a one-line record,
 * and a draft Claude replaced reads as one quiet "Earlier version" row.
 */
import { ChevronDown, ChevronUp, Send } from "lucide-react";
import { useEffect, useId, useRef, useState, type ReactNode, type Ref } from "react";

import { CardFoot, DeclineStep, EditableText, PartLabel, useApproveShortcut } from "../../cards";
import { clockTime } from "../../requests/format";
import { Button, IconButton } from "../../shell/ui/Button";
import { cx } from "../../shell/ui/cx";
import { BudgetStrip } from "../BudgetStrip";
import { plural } from "../format";
import {
  approveBody,
  askText,
  BRIEF_PART,
  hasClearedPart,
  hasEdits,
  PART_MAX_CHARS,
  showVersion,
  shownVersion,
  type OutreachDraft,
  type PartVersions,
} from "../outreachDraft";
import { FitBadge, OrgAvatar, Pill } from "../parts";
import { outreachRecipients } from "../recipients";
import { SendModeLine, type SendModeProps } from "../SendMode";
import type { ApproveCard, CardRecipient, OutreachCardItem } from "../types";

/**
 * How long a card that turned up while the member was on the page ignores Approve, so
 * a click or ⌘↵ meant for what was there before can't send it.
 */
export const ARRIVAL_PAUSE_MS = 600;

export type DraftUpdate = (update: (draft: OutreachDraft) => OutreachDraft) => void;

export interface OutreachCardProps extends SendModeProps {
  item: OutreachCardItem;
  /** Claude's wordings of each part across the drafts this one replaced. */
  versions: PartVersions;
  /** The member's ticks and wording, kept by the chat so folding and refetches keep them. */
  draft: OutreachDraft;
  onDraftChange: DraftUpdate;
  folded: boolean;
  onFoldChange: (folded: boolean) => void;
  /** Approve or Decline is on its way. */
  busy: boolean;
  /** The Decline step is open in the foot's place. */
  declineOpen: boolean;
  onDeclineOpenChange: (open: boolean) => void;
  /** Decline is the one on its way: its step stays, locked. */
  declinePending: boolean;
  error: string | null;
  /** Why live turns can't start (`budgetLine`): both buttons hold; ticks and wording still work. */
  blocked: string | null;
  /** The card turned up while the member was on the page (`ARRIVAL_PAUSE_MS`). */
  arriving: boolean;
  onApprove: (body: ApproveCard) => void;
  onDecline: (reason: string | null) => void;
  /** The card's outer box, which the chat scrolls into view and watches for the Review strip. */
  ref?: Ref<HTMLDivElement>;
}

/** "A", "A and B", "A, B and C". */
function names(recipients: readonly CardRecipient[]): string {
  const all = recipients.map((recipient) => recipient.org.name);
  return all.length <= 1 ? (all[0] ?? "") : `${all.slice(0, -1).join(", ")} and ${all.at(-1) ?? ""}`;
}

function routeLine(item: OutreachCardItem, chosen: ReadonlySet<string>, proposed: ReadonlySet<string>): string {
  const picked =
    item.screened === null
      ? `Claude picked ${plural(proposed.size, "organization")}`
      : `Claude screened ${plural(item.screened.total, "organization")} and picked ${String(proposed.size)}`;
  const same = chosen.size === proposed.size && [...chosen].every((id) => proposed.has(id));
  return same ? `${picked}.` : `${picked}; you've chosen ${String(chosen.size)}.`;
}

/** What Approve and send does, under the buttons; or why it can't yet. */
function approveHint(item: OutreachCardItem, draft: OutreachDraft, blocked: string | null): string {
  if (blocked !== null) return "Nothing goes out until the budget is back";
  const going = item.recipients.filter((recipient) => draft.chosen.has(recipient.org.id));
  if (going.length === 0) return "Tick at least one organization";
  if (hasClearedPart(item, draft)) return "The brief and each ask need some text";
  const who =
    going.length === 1
      ? `${names(going)}${item.recipients.length > 1 ? " only" : ""}`
      : going.length === item.recipients.length
        ? "each of them"
        : names(going);
  return `Starts a conversation with ${who}${hasEdits(item, draft) ? ", with your edits" : ""} · ⌘↵`;
}

/** The card's top line: what it is, then its state or what it needs, and the fold control. */
function Eyebrow({ labelId, children, control }: { labelId: string; children?: ReactNode; control?: ReactNode }) {
  return (
    <div className="flex min-h-7 items-center gap-1.5 text-ui-sm text-muted">
      <Send size={15} strokeWidth={1.75} aria-hidden className="shrink-0" />
      <span id={labelId} className="whitespace-nowrap text-secondary">
        Harmony Network Request
      </span>
      {children}
      <span className="flex-1" />
      {control}
    </div>
  );
}

/**
 * A section's label with a quiet hint on its right, hidden once the section shows marks
 * of its own. Where both don't fit on one line the hint wraps under the label, so the
 * label never folds into a column beside it.
 */
function SectionHead({ hint, children }: { hint?: string | null; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5">
      <div className="min-w-0 flex-auto">{children}</div>
      {hint && <span className="ml-auto text-ui-sm whitespace-nowrap text-faint">{hint}</span>}
    </div>
  );
}

/** One editable part (the brief, or an ask): its label, marks and pager, and the text. */
function EditablePart({
  label,
  caption,
  fieldLabel,
  part,
  versions: all,
  value,
  draft,
  onDraftChange,
  onChange,
  hint,
}: {
  label: string;
  caption: string;
  fieldLabel: string;
  /** `BRIEF_PART`, or the org id of an ask. */
  part: string;
  versions: PartVersions;
  value: string;
  draft: OutreachDraft;
  onDraftChange: DraftUpdate;
  onChange: (text: string) => void;
  hint?: string;
}) {
  const versions = part === BRIEF_PART ? all.brief : (all.asks[part] ?? []);
  const shown = shownVersion(versions, draft, part);
  const edited = value !== shown.text;
  const paged = versions.length > 1;
  const page = (index: number) => {
    onDraftChange((current) => showVersion(current, all, part, index));
  };
  return (
    <div>
      <SectionHead hint={edited || paged ? null : hint}>
        <PartLabel
          label={label}
          caption={caption}
          rewritten={
            paged
              ? {
                  index: shown.index,
                  count: versions.length,
                  onPrev: () => {
                    page(shown.index - 1);
                  },
                  onNext: () => {
                    page(shown.index + 1);
                  },
                }
              : undefined
          }
          edited={
            edited
              ? {
                  onUndo: () => {
                    onChange(shown.text);
                  },
                }
              : undefined
          }
        />
      </SectionHead>
      <div className="mt-1.5">
        <EditableText
          value={value}
          original={shown.text}
          onChange={onChange}
          label={fieldLabel}
          maxLength={PART_MAX_CHARS}
        />
      </div>
    </div>
  );
}

function RecipientRow({
  recipient,
  proposed,
  versions,
  draft,
  onDraftChange,
}: {
  recipient: CardRecipient;
  proposed: boolean;
  versions: PartVersions;
  draft: OutreachDraft;
  onDraftChange: DraftUpdate;
}) {
  const orgId = recipient.org.id;
  const name = recipient.org.name;
  const ticked = draft.chosen.has(orgId);
  const hasAsk = (versions.asks[orgId] ?? []).length > 0;
  const toggle = () => {
    onDraftChange((current) => {
      const chosen = new Set(current.chosen);
      if (chosen.has(orgId)) chosen.delete(orgId);
      else chosen.add(orgId);
      return { ...current, chosen };
    });
  };
  return (
    <li className="pt-3.5 [&+&]:mt-3.5 [&+&]:border-t [&+&]:border-dashed [&+&]:border-border">
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
        <label className="flex min-w-0 cursor-pointer items-center gap-2.5">
          <input type="checkbox" checked={ticked} onChange={toggle} className="size-4 shrink-0 accent-ink" />
          <span className={cx("flex min-w-0 items-center gap-2.5", !ticked && "opacity-50")}>
            <OrgAvatar org={recipient.org} size={22} />
            <span className="min-w-0 text-ui font-medium break-words text-ink">{name}</span>
          </span>
        </label>
        <span className={cx(!ticked && "opacity-50")}>
          <FitBadge fit={recipient.fit} />
        </span>
      </div>
      <div className="pl-[26px]">
        {recipient.reason !== "" && (
          <p className={cx("mt-1 text-ui-sm text-muted", !ticked && "opacity-50")}>{recipient.reason}</p>
        )}
        {ticked ? (
          <>
            {recipient.title && (
              <div className="mt-3">
                <PartLabel label="Subject" caption={`${name} sees this as written`} />
                <p className="mt-1 text-ui text-ink">{recipient.title}</p>
              </div>
            )}
            {hasAsk && (
              <div className="mt-3">
                <EditablePart
                  label="Ask"
                  caption={`Claude works from this with ${name} only`}
                  fieldLabel={`Ask for ${name}`}
                  part={orgId}
                  versions={versions}
                  value={askText(draft, recipient)}
                  draft={draft}
                  onDraftChange={onDraftChange}
                  onChange={(text) => {
                    onDraftChange((current) => ({ ...current, asks: { ...current.asks, [orgId]: text } }));
                  }}
                />
              </div>
            )}
          </>
        ) : (
          proposed && (
            <p className="mt-2 text-ui-sm text-muted">
              Left out: {name} gets nothing from this request. Tick it to add it back.
            </p>
          )
        )}
      </div>
    </li>
  );
}

/** The card waiting on the member. */
export function OutreachCard({
  item,
  versions,
  draft,
  onDraftChange,
  sendMode,
  onSendModeChange,
  folded,
  onFoldChange,
  busy,
  declineOpen,
  onDeclineOpenChange,
  declinePending,
  error,
  blocked,
  arriving,
  onApprove,
  onDecline,
  ref,
}: OutreachCardProps) {
  const labelId = useId();
  const titleId = useId();
  const cardRef = useRef<HTMLElement | null>(null);
  const [armed, setArmed] = useState(!arriving);
  const { proposed } = outreachRecipients(item);

  useEffect(() => {
    if (armed) return;
    const timer = setTimeout(() => {
      setArmed(true);
    }, ARRIVAL_PAUSE_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [armed]);

  const canApprove =
    armed && !busy && blocked === null && draft.chosen.size > 0 && !hasClearedPart(item, draft);
  const approve = () => {
    if (canApprove) onApprove(approveBody(item, draft));
  };
  useApproveShortcut(cardRef, approve, !folded && !declineOpen && canApprove);

  const going = item.recipients.filter((recipient) => draft.chosen.has(recipient.org.id));
  const title = item.title ?? "Request to the network";

  return (
    // Not a scroll anchor: a budget strip turning up on the card's top edge shows where
    // the card was, rather than above the view while the card holds still.
    <div ref={ref} className="[overflow-anchor:none]">
      {blocked !== null && <BudgetStrip line={blocked} />}
      <section
        ref={cardRef}
        aria-labelledby={`${labelId} ${titleId}`}
        className="relative rounded-menu border border-border-strong bg-surface"
      >
        <div className={cx("px-4 pt-3", folded ? "pb-3.5" : "pb-4")}>
          {folded ? (
            <Eyebrow
              labelId={labelId}
              control={
                <Button
                  variant="ghost"
                  size="sm"
                  aria-expanded={false}
                  className="-mr-2"
                  onClick={() => {
                    onFoldChange(false);
                  }}
                >
                  Show
                  <ChevronDown size={15} strokeWidth={1.75} aria-hidden />
                </Button>
              }
            >
              <Pill tone="warning">Waiting for you</Pill>
            </Eyebrow>
          ) : (
            <Eyebrow
              labelId={labelId}
              control={
                <IconButton
                  label="Collapse"
                  size="sm"
                  aria-expanded
                  className="-mr-2"
                  onClick={() => {
                    onFoldChange(true);
                  }}
                >
                  <ChevronUp size={16} strokeWidth={1.75} />
                </IconButton>
              }
            >
              <span className="min-w-0 truncate">· Claude needs your OK before sending</span>
            </Eyebrow>
          )}
          <h2 id={titleId} className="mt-1 text-ui-title font-medium break-words text-ink">
            {title}
          </h2>
          {folded ? (
            <p className="mt-1 text-ui-sm text-muted">
              {going.length === 0 ? "No organization ticked" : `To ${names(going)}`}
            </p>
          ) : (
            <>
              <p className="mt-1 text-ui-sm text-muted">{routeLine(item, draft.chosen, proposed)}</p>
              <div className="mt-1">
                <SendModeLine sendMode={sendMode} onSendModeChange={onSendModeChange} />
              </div>

              <div className="mt-3.5 border-t border-border pt-3.5">
                <EditablePart
                  label="Brief"
                  caption="Claude works from this in every conversation"
                  fieldLabel="Brief"
                  part={BRIEF_PART}
                  versions={versions}
                  value={draft.brief}
                  draft={draft}
                  onDraftChange={onDraftChange}
                  onChange={(text) => {
                    onDraftChange((current) => ({ ...current, brief: text }));
                  }}
                  hint="Click the brief or an ask to change the wording"
                />
              </div>

              <div className="mt-3.5 border-t border-border pt-3.5">
                <SectionHead hint={item.recipients.length > 1 ? "Untick to leave one out" : null}>
                  <PartLabel
                    label="Who gets it"
                    caption={`${String(draft.chosen.size)} of ${String(item.recipients.length)}`}
                  />
                </SectionHead>
                <ul>
                  {item.recipients.map((recipient) => (
                    <RecipientRow
                      key={recipient.org.id}
                      recipient={recipient}
                      proposed={proposed.has(recipient.org.id)}
                      versions={versions}
                      draft={draft}
                      onDraftChange={onDraftChange}
                    />
                  ))}
                </ul>
              </div>
            </>
          )}
          {!folded && declineOpen && error !== null && (
            <p role="alert" className="mt-3.5 text-ui-sm text-danger">
              {error}
            </p>
          )}
        </div>
        {!folded &&
          (declineOpen ? (
            <DeclineStep
              required={false}
              title="Don't send this request"
              caption="Only Claude in this chat sees this. No organization hears about it."
              pending={declinePending || blocked !== null}
              onCancel={() => {
                onDeclineOpenChange(false);
              }}
              onConfirm={onDecline}
            />
          ) : (
            <CardFoot
              onDecline={() => {
                onDeclineOpenChange(true);
              }}
              onApprove={approve}
              approveDisabled={!canApprove}
              declineDisabled={busy || blocked !== null}
              hintLeft={
                error === null ? undefined : (
                  <span role="alert" className="text-danger">
                    {error}
                  </span>
                )
              }
              hintRight={approveHint(item, draft, blocked)}
            />
          ))}
      </section>
    </div>
  );
}

// --- After the decision ------------------------------------------------------------------

/** Who a decided card went, or would have gone, to. */
function goesTo(item: OutreachCardItem): CardRecipient[] {
  if (item.status === "approved" || item.auto) return item.recipients.filter((recipient) => recipient.selected);
  const { proposed } = outreachRecipients(item);
  return item.recipients.filter((recipient) => proposed.has(recipient.org.id));
}

/** The card's text, read-only: what went out (or would have), and to whom. */
function OutreachBody({ item, sent }: { item: OutreachCardItem; sent: boolean }) {
  const going = goesTo(item);
  const left = item.recipients.filter((recipient) => !going.includes(recipient));
  return (
    <div className="mt-3.5 border-t border-border pt-3.5">
      <PartLabel label="Brief" />
      <p className="mt-1.5 font-serif text-ui-md break-words whitespace-pre-wrap text-ink">{item.brief}</p>
      <ul className="mt-3.5 border-t border-border">
        {going.map((recipient) => (
          <li key={recipient.org.id} className="pt-3.5 [&+&]:mt-3.5 [&+&]:border-t [&+&]:border-dashed [&+&]:border-border">
            <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
              <OrgAvatar org={recipient.org} size={22} />
              <span className="min-w-0 text-ui font-medium break-words text-ink">{recipient.org.name}</span>
              <FitBadge fit={recipient.fit} />
            </div>
            {recipient.title && (
              <div className="mt-3">
                <PartLabel label="Subject" />
                <p className="mt-1 text-ui text-ink">{recipient.title}</p>
              </div>
            )}
            {recipient.ask && (
              <div className="mt-3">
                <PartLabel label="Ask" />
                <p className="mt-1 font-serif text-ui-md break-words whitespace-pre-wrap text-ink">{recipient.ask}</p>
              </div>
            )}
          </li>
        ))}
      </ul>
      {left.length > 0 && (
        <p className="mt-3.5 text-ui-sm text-muted">
          {sent ? "Not sent to" : "Not for"} {names(left)}.
        </p>
      )}
    </div>
  );
}

function recordState(item: OutreachCardItem, sentAt: string | null, edited: boolean): { pill: ReactNode; route: string } {
  const to = names(goesTo(item));
  const at = sentAt === null ? "" : ` · ${clockTime(sentAt)}`;
  if (item.auto) {
    switch (item.status) {
      case "pending":
        return { pill: <Pill tone="info" pulse>Sending…</Pill>, route: `To ${to}` };
      case "approved":
        return { pill: <Pill tone="success">Sent without review</Pill>, route: `Sent to ${to}${at}` };
      case "rejected":
      case "superseded":
        return {
          pill: <Pill tone="muted">Not sent</Pill>,
          route: item.feedback === null ? "Not sent" : `Not sent: ${item.feedback}`,
        };
    }
  }
  return item.status === "approved"
    ? { pill: <Pill tone="success">Sent</Pill>, route: `Sent to ${to}${edited ? ", with your edits" : ""}${at}` }
    : { pill: <Pill tone="muted">Declined</Pill>, route: "Not sent" };
}

/**
 * A decided card, as it stays in the transcript: one line saying what happened, and
 * Show to read what was sent in full. `edited`: the member sent wording of their own.
 */
export function OutreachRecord({
  item,
  sentAt,
  edited = false,
}: {
  item: OutreachCardItem;
  sentAt: string | null;
  edited?: boolean;
}) {
  const labelId = useId();
  const titleId = useId();
  const [open, setOpen] = useState(false);
  const { pill, route } = recordState(item, sentAt, edited);
  const sent = item.status === "approved";
  return (
    <section
      aria-labelledby={`${labelId} ${titleId}`}
      className="rounded-menu border border-border-strong bg-surface px-4 pt-3 pb-3.5"
    >
      <Eyebrow
        labelId={labelId}
        control={
          <Button
            variant="ghost"
            size="sm"
            aria-expanded={open}
            className="-mr-2"
            onClick={() => {
              setOpen((current) => !current);
            }}
          >
            {open ? "Hide" : sent ? "Show what was sent" : "Show"}
            {open ? (
              <ChevronUp size={15} strokeWidth={1.75} aria-hidden />
            ) : (
              <ChevronDown size={15} strokeWidth={1.75} aria-hidden />
            )}
          </Button>
        }
      >
        {pill}
      </Eyebrow>
      <h3 id={titleId} className="mt-1 text-ui-title font-medium break-words text-ink">
        {item.title ?? "Request to the network"}
      </h3>
      <p className="mt-1 text-ui-sm break-words text-muted">{route}</p>
      {open && (
        <>
          <OutreachBody item={item} sent={sent} />
          {!item.auto && item.status === "rejected" && item.feedback !== null && (
            <p className="mt-3.5 text-ui-sm text-muted">
              Your reason, which only Claude in this chat saw: <span className="text-secondary">{item.feedback}</span>
            </p>
          )}
        </>
      )}
    </section>
  );
}

/** A draft Claude replaced: one quiet row in the transcript, with Show to read it. */
export function EarlierVersion({ item }: { item: OutreachCardItem }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <p className="flex items-start gap-2 text-ui-sm text-muted">
        <Send size={15} strokeWidth={1.75} aria-hidden className="mt-px shrink-0" />
        <span className="min-w-0">
          <span className="font-medium text-secondary">Earlier version</span> of this request · not sent ·{" "}
          <button
            type="button"
            aria-expanded={open}
            onClick={() => {
              setOpen((current) => !current);
            }}
            className="rounded-[4px] text-link outline-none hover:underline focus-visible:underline"
          >
            {open ? "Hide" : "Show"}
          </button>
        </span>
      </p>
      {open && (
        <div className="mt-2 rounded-menu border border-border bg-surface px-4 pt-3 pb-3.5">
          <p className="text-ui-title font-medium break-words text-ink">{item.title ?? "Request to the network"}</p>
          <OutreachBody item={item} sent={false} />
        </div>
      )}
    </div>
  );
}
