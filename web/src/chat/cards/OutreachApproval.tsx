import { useId, useState, type ReactNode } from "react";

import { Button } from "../../shell/ui/Button";
import { ClampedText } from "../../shell/ui/ClampedText";
import { BRIEF_MAX_CHARS, briefGist, editedText } from "../brief";
import { budgetLine, useBudgetState } from "../failureCopy";
import { plural } from "../format";
import { approveBody, askText, hasEdits, outreachTitle, type OutreachDraft } from "../outreachDraft";
import { FitBadge, OrgAvatar } from "../parts";
import { outreachRecipients } from "../recipients";
import type { ApproveCard, CardRecipient, OutreachCardItem } from "../types";
import { AskUserCard, type AskOption } from "./AskUserCard";
import { ExpandingTextarea } from "./ExpandingTextarea";
import { RecipientAsk } from "./RecipientAsk";

type Mode = "choose" | "subset" | "edit";

/** Collapsed, each of the edit view's boxes shows at most this many lines. */
const EDIT_ROWS = 7;

export type DraftUpdate = (update: (current: OutreachDraft) => OutreachDraft) => void;

export interface OutreachApprovalProps {
  item: OutreachCardItem;
  busy: boolean;
  error: string | null;
  onApprove: (body: ApproveCard) => void;
  /** Who is ticked and what was reworded, kept by the caller so putting the card away keeps it. */
  draft: OutreachDraft;
  onDraftChange: DraftUpdate;
  /** The "Something else" text: Claude gets it as the reason the card was rejected. */
  feedback: string;
  onFeedbackChange: (text: string) => void;
  onReject: (feedback: string) => void;
  onDismiss: () => void;
  /** See `AskUserCard`. */
  arriving?: boolean;
}

function PanelFooter({ onBack, children }: { onBack: () => void; children: ReactNode }) {
  return (
    <div className="flex items-center justify-end gap-2 border-t border-border px-3 pt-3 pb-1">
      <Button variant="ghost" onClick={onBack}>
        Back
      </Button>
      {children}
    </div>
  );
}

/**
 * A panel's own content: as tall as it needs, up to what leaves the transcript above
 * readable, after which the panel (never a field inside it) scrolls.
 */
function PanelBody({ children }: { children: ReactNode }) {
  return <div className="scrollbar-quiet max-h-[min(56vh,560px)] overflow-y-auto">{children}</div>;
}

function SubsetPanel({
  item,
  busy,
  draft,
  onDraftChange,
  onBack,
  onSend,
}: {
  item: OutreachCardItem;
  /** A call is in flight, or a spend cap holds the send. */
  busy: boolean;
  draft: OutreachDraft;
  onDraftChange: DraftUpdate;
  onBack: () => void;
  onSend: () => void;
}) {
  const toggle = (orgId: string) => {
    onDraftChange((current) => {
      const chosen = new Set(current.chosen);
      if (chosen.has(orgId)) chosen.delete(orgId);
      else chosen.add(orgId);
      return { ...current, chosen };
    });
  };
  return (
    <div className="px-2 pb-2">
      <PanelBody>
        <p className="px-3 pb-1.5 text-[13px] text-muted">Choose who receives the request.</p>
        {item.recipients.map((recipient) => (
          <div key={recipient.org.id} className="rounded-xl px-3 py-2.5 transition-colors hover:bg-hover/70">
            <label className="flex cursor-pointer items-center gap-3">
              <input
                type="checkbox"
                checked={draft.chosen.has(recipient.org.id)}
                onChange={() => {
                  toggle(recipient.org.id);
                }}
                className="size-4 shrink-0 accent-accent-strong"
              />
              <OrgAvatar org={recipient.org} size={22} />
              <span className="min-w-0 flex-1 truncate text-[15px] text-ink">{recipient.org.name}</span>
              <FitBadge fit={recipient.fit} />
            </label>
            <RecipientAsk title={recipient.title} ask={askText(draft, recipient)} className="mt-2 ml-[62px]" />
          </div>
        ))}
      </PanelBody>
      <PanelFooter onBack={onBack}>
        <Button variant="primary" disabled={busy || draft.chosen.size === 0} onClick={onSend}>
          Send to {plural(draft.chosen.size, "organization")}
        </Button>
      </PanelFooter>
    </div>
  );
}

function AskField({
  recipient,
  draft,
  onDraftChange,
}: {
  recipient: CardRecipient;
  draft: OutreachDraft;
  onDraftChange: DraftUpdate;
}) {
  const orgId = recipient.org.id;
  return (
    <div>
      <div className="flex items-center gap-2">
        <OrgAvatar org={recipient.org} size={20} />
        <span className="min-w-0 flex-1 truncate text-[13.5px] font-medium text-ink">{recipient.org.name}</span>
      </div>
      {recipient.title && (
        <p className="mt-1 text-[13px] leading-5 text-muted">
          Subject: <span className="text-secondary">{recipient.title}</span>
        </p>
      )}
      <ExpandingTextarea
        aria-label={`Ask for ${recipient.org.name}`}
        className="mt-1.5"
        maxRows={EDIT_ROWS}
        maxLength={BRIEF_MAX_CHARS}
        placeholder="What to ask this organization only"
        value={askText(draft, recipient)}
        onChange={(text) => {
          onDraftChange((current) => ({ ...current, asks: { ...current.asks, [orgId]: text } }));
        }}
      />
    </div>
  );
}

function EditPanel({
  item,
  busy,
  draft,
  onDraftChange,
  onBack,
  onSend,
}: {
  item: OutreachCardItem;
  /** As `SubsetPanel`'s. */
  busy: boolean;
  draft: OutreachDraft;
  onDraftChange: DraftUpdate;
  onBack: () => void;
  onSend: () => void;
}) {
  const briefId = useId();
  const hintId = useId();
  const ticked = item.recipients.filter((recipient) => draft.chosen.has(recipient.org.id));
  return (
    <div className="px-2 pb-2">
      <PanelBody>
        <div className="flex flex-col gap-4 px-3 pt-1 pb-3">
          <div>
            <label htmlFor={briefId} className="mb-1.5 block text-[13px] font-medium text-secondary">
              Brief
            </label>
            <ExpandingTextarea
              id={briefId}
              aria-describedby={hintId}
              maxRows={EDIT_ROWS}
              maxLength={BRIEF_MAX_CHARS}
              value={draft.brief}
              onChange={(brief) => {
                onDraftChange((current) => ({ ...current, brief }));
              }}
            />
            <p id={hintId} className="mt-1.5 text-[12.5px] leading-5 text-muted">
              Every organization&apos;s conversation starts from this brief. A budget or other limit goes in each
              organization&apos;s ask, which only your representative in that conversation sees.
            </p>
          </div>
          {ticked.map((recipient) => (
            <AskField key={recipient.org.id} recipient={recipient} draft={draft} onDraftChange={onDraftChange} />
          ))}
        </div>
      </PanelBody>
      <PanelFooter onBack={onBack}>
        <Button variant="primary" disabled={busy || draft.chosen.size === 0 || !hasEdits(item, draft)} onClick={onSend}>
          Send with changes
        </Button>
      </PanelFooter>
    </div>
  );
}

function sendLabel(chosen: number, listed: number): string {
  if (chosen < listed) return `Send to ${plural(chosen, "organization")}`;
  return chosen === 1 ? "Send" : `Send to all ${chosen}`;
}

/**
 * The `send_network_request` approval as Claude's ask-user card: send to the ticked
 * organizations (at first, those Claude suggested), to a subset, or with the member's
 * own wording of the brief and the asks. Typing in "Something else" (or the composer)
 * rejects the card with that text as feedback, as in Claude Code.
 *
 * Sending starts the request's threads, whose first turns a spent cap or a pause would
 * refuse, ending them for good: while the budget isn't `ok`, every send holds, with the
 * composer's line on the card (LIVE_DEMO_PLAN 3.4). Choosing and rewording still work.
 */
export function OutreachApproval({
  item,
  busy,
  error,
  onApprove,
  draft,
  onDraftChange,
  feedback,
  onFeedbackChange,
  onReject,
  onDismiss,
  arriving = false,
}: OutreachApprovalProps) {
  const [mode, setMode] = useState<Mode>("choose");
  const titleId = useId();
  const blocked = budgetLine(useBudgetState());
  const { listed } = outreachRecipients(item);
  const names = item.recipients
    .filter((recipient) => draft.chosen.has(recipient.org.id))
    .map((recipient) => recipient.org.name)
    .join(", ");

  // Whichever way it's sent, the member's other choices go with it.
  const approve = () => {
    if (blocked === null) onApprove(approveBody(item, draft));
  };

  const options: AskOption[] = [
    draft.chosen.size === 0
      ? {
          id: "all",
          label: "Choose who receives it…",
          hint: "No organization is ticked",
          onSelect: () => {
            setMode("subset");
          },
        }
      : {
          id: "all",
          label: sendLabel(draft.chosen.size, listed.size),
          hint: names,
          onSelect: approve,
          startsTurn: true,
        },
  ];
  if (item.recipients.length > 1) {
    options.push({
      id: "subset",
      label: "Send to a subset…",
      hint: "Choose which organizations receive it",
      onSelect: () => {
        setMode("subset");
      },
    });
  }
  options.push({
    id: "edit",
    label: "Edit brief…",
    hint: "Change what your representative asks each organization",
    onSelect: () => {
      setMode("edit");
    },
  });

  const back = () => {
    setMode("choose");
  };
  const panel =
    mode === "subset" ? (
      <SubsetPanel
        item={item}
        busy={busy || blocked !== null}
        draft={draft}
        onDraftChange={onDraftChange}
        onBack={back}
        onSend={approve}
      />
    ) : mode === "edit" ? (
      <EditPanel
        item={item}
        busy={busy || blocked !== null}
        draft={draft}         onDraftChange={onDraftChange}
        onBack={back}
        onSend={approve}
      />
    ) : undefined;

  // The whole brief is in the transcript card right above; this is its opening.
  const gist = briefGist(editedText(item.brief, draft.brief) ?? item.brief);
  return (
    <AskUserCard
      labelledBy={titleId}
      title={mode === "edit" ? "Edit the request before it goes out" : outreachTitle(draft)}
      options={options}
      other={feedback}
      onOtherChange={onFeedbackChange}
      onOther={onReject}
      otherPlaceholder="Something else"
      onDismiss={onDismiss}
      busy={busy}
      blocked={blocked}
      error={error}
      panel={panel}
      onBack={back}
      arriving={arriving}
    >
      {mode === "edit" ? undefined : (
        // Keyed on the text: the clamp measures on resize, and new text may not resize it.
        <ClampedText key={gist} lines={3} className="-mt-1 text-[13.5px] leading-5 text-muted">
          {gist}
        </ClampedText>
      )}
    </AskUserCard>
  );
}
