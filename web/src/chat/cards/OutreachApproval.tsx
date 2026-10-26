import { useId, useState, type ReactNode } from "react";

import { ScriptedNote } from "../../demo/ScriptedNote";
import { Button } from "../../shell/ui/Button";
import { BRIEF_MAX_CHARS, briefGist, editedBrief } from "../brief";
import { plural } from "../format";
import { FitBadge, OrgAvatar } from "../parts";
import { outreachRecipients } from "../recipients";
import type { ApproveCard, OutreachCardItem } from "../types";
import { AskUserCard, type AskOption } from "./AskUserCard";

type Mode = "choose" | "subset" | "edit";

export interface OutreachApprovalProps {
  item: OutreachCardItem;
  busy: boolean;
  error: string | null;
  onApprove: (body: ApproveCard) => void;
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

function SubsetPanel({
  item,
  busy,
  chosen,
  onChosenChange,
  onBack,
  onSend,
}: {
  item: OutreachCardItem;
  busy: boolean;
  chosen: ReadonlySet<string>;
  onChosenChange: (update: (current: ReadonlySet<string>) => ReadonlySet<string>) => void;
  onBack: () => void;
  onSend: () => void;
}) {
  const toggle = (orgId: string) => {
    onChosenChange((current) => {
      const next = new Set(current);
      if (next.has(orgId)) next.delete(orgId);
      else next.add(orgId);
      return next;
    });
  };
  return (
    <div className="px-2 pb-2">
      <p className="px-3 pb-1.5 text-[13px] text-muted">Choose who receives the request.</p>
      {item.recipients.map((recipient) => (
        <label
          key={recipient.org.id}
          className="flex cursor-pointer items-center gap-3 rounded-xl px-3 py-2.5 transition-colors hover:bg-hover/70"
        >
          <input
            type="checkbox"
            checked={chosen.has(recipient.org.id)}
            onChange={() => {
              toggle(recipient.org.id);
            }}
            className="size-4 shrink-0 accent-accent-strong"
          />
          <OrgAvatar org={recipient.org} size={22} />
          <span className="min-w-0 flex-1 truncate text-[15px] text-ink">{recipient.org.name}</span>
          <FitBadge fit={recipient.fit} />
        </label>
      ))}
      <PanelFooter onBack={onBack}>
        <Button variant="primary" disabled={busy || chosen.size === 0} onClick={onSend}>
          Send to {plural(chosen.size, "organization")}
        </Button>
      </PanelFooter>
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
  busy: boolean;
  draft: string;
  onDraftChange: (text: string) => void;
  onBack: () => void;
  onSend: () => void;
}) {
  const hintId = useId();
  return (
    <div className="px-2 pb-2">
      <div className="px-3 pt-1 pb-3">
        <textarea
          aria-label="Brief"
          aria-describedby={hintId}
          rows={7}
          maxLength={BRIEF_MAX_CHARS}
          value={draft}
          onChange={(event) => {
            onDraftChange(event.target.value);
          }}
          className="scrollbar-quiet block max-h-[min(42vh,360px)] w-full resize-none rounded-control border border-border-strong bg-surface px-3 py-2 text-[14px] leading-[1.55] text-ink outline-none transition-colors placeholder:text-faint focus:border-accent"
        />
        <p id={hintId} className="mt-1.5 text-[12.5px] leading-5 text-muted">
          Your representative works from this brief. It tells the recipients what they need to answer, and it is
          asked to keep the rest, such as a budget, to itself unless the brief says otherwise.
        </p>
        <ScriptedNote className="mt-1" />
      </div>
      <PanelFooter onBack={onBack}>
        <Button
          variant="primary"
          disabled={busy || editedBrief(item.brief, draft) === null}
          onClick={onSend}
        >
          Send with changes
        </Button>
      </PanelFooter>
    </div>
  );
}

function sameSet(a: ReadonlySet<string>, b: ReadonlySet<string>): boolean {
  return a.size === b.size && [...a].every((value) => b.has(value));
}

/**
 * The `send_network_request` approval as Claude's ask-user card: send as Claude
 * proposed, to a subset, or with the member's own wording of the brief. Typing in
 * "Something else" (or the composer) rejects the card with that text as feedback, as
 * in Claude Code.
 */
export function OutreachApproval({
  item,
  busy,
  error,
  onApprove,
  feedback,
  onFeedbackChange,
  onReject,
  onDismiss,
  arriving = false,
}: OutreachApprovalProps) {
  const [mode, setMode] = useState<Mode>("choose");
  const { listed: everyone, proposed } = outreachRecipients(item);
  // Kept here, so Back (or Esc) and coming back to a panel doesn't lose the choices.
  const [chosen, setChosen] = useState<ReadonlySet<string>>(proposed);
  const [draft, setDraft] = useState(item.brief);
  const titleId = useId();
  const names = item.recipients
    .filter((recipient) => proposed.has(recipient.org.id))
    .map((recipient) => recipient.org.name)
    .join(", ");

  // Whichever way it's sent, the member's other choices go with it.
  const approve = (recipients: ReadonlySet<string>) => {
    const brief = editedBrief(item.brief, draft);
    onApprove({
      ...(sameSet(recipients, everyone)
        ? {}
        : { recipients: item.recipients.map((r) => r.org.id).filter((orgId) => recipients.has(orgId)) }),
      ...(brief === null ? {} : { brief }),
    });
  };

  const options: AskOption[] = [
    {
      id: "all",
      label:
        proposed.size < everyone.size
          ? `Send to ${plural(proposed.size, "organization")}`
          : proposed.size === 1
            ? "Send"
            : `Send to all ${proposed.size}`,
      hint: names,
      onSelect: () => {
        approve(proposed);
      },
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
    hint: "Change what your representative asks for",
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
        busy={busy}
        chosen={chosen}
        onChosenChange={setChosen}
        onBack={back}
        onSend={() => {
          approve(chosen);
        }}
      />
    ) : mode === "edit" ? (
      <EditPanel
        item={item}
        busy={busy}
        draft={draft}
        onDraftChange={setDraft}
        onBack={back}
        onSend={() => {
          approve(chosen);
        }}
      />
    ) : undefined;

  return (
    <AskUserCard
      labelledBy={titleId}
      title={mode === "edit" ? "Edit the brief before it goes out" : `Send this request to ${plural(proposed.size, "organization")}?`}
      options={options}
      other={feedback}
      onOtherChange={onFeedbackChange}
      onOther={onReject}
      otherPlaceholder="Something else"
      onDismiss={onDismiss}
      busy={busy}
      error={error}
      panel={panel}
      onBack={back}
      arriving={arriving}
    >
      {mode === "choose" ? (
        // The whole brief is in the transcript card right above; this is its opening.
        <p className="-mt-1 line-clamp-2 text-[13.5px] leading-5 text-muted">
          {briefGist(editedBrief(item.brief, draft) ?? item.brief)}
        </p>
      ) : undefined}
    </AskUserCard>
  );
}
