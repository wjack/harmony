import { Pencil, RotateCcw } from "lucide-react";
import { useEffect, useState, type KeyboardEvent } from "react";

import { useAutoHeight } from "../requests/autoHeight";
import { Button } from "../shell/ui/Button";
import { cx } from "../shell/ui/cx";
import { UserMessage } from "./Messages";

/** The editor grows with the text up to this height, then scrolls. */
const MAX_EDITOR_PX = 320;

export interface DeclinedTurnProps {
  /** What the member sent, one entry per message, in order. */
  texts: readonly string[];
  /** "Claude declined to respond to this (biosecurity)." */
  note: string;
  /** A Retry or an edit is on its way. */
  busy: boolean;
  /** Why the last Retry or edit didn't go through. */
  error: string | null;
  /** Why live turns can't start now (`budgetLine`): both wait until it clears. */
  blocked: string | null;
  /** Send the messages again: as they were (`null`), or as the member reworded them. */
  onRetry: (text: string | null) => void;
}

/**
 * The member's messages Claude declined to respond to, with a note under them that says
 * so and two ways on, as Claude does under a member message: Retry sends them again as
 * they were, and Edit turns them into one box to reword before sending. Either starts a
 * new turn.
 */
export function DeclinedTurn({ texts, note, busy, error, blocked, onRetry }: DeclinedTurnProps) {
  const original = texts.join("\n\n");
  const [editing, setEditing] = useState<string | null>(null);
  const field = useAutoHeight(editing ?? "", MAX_EDITOR_PX);
  const opened = editing !== null;

  // Opening the editor puts the cursor after the text, as the composer does.
  useEffect(() => {
    const element = field.current;
    if (!opened || element === null) return;
    element.focus();
    element.setSelectionRange(element.value.length, element.value.length);
  }, [opened, field]);

  const failure = error !== null && <p role="alert" className="mt-1.5 text-right text-[13px] text-danger">{error}</p>;

  if (editing !== null) {
    const reworded = editing.trim();
    const canSend = reworded !== "" && !busy && blocked === null;
    const send = () => {
      // Unchanged wording is a Retry, which keeps each message as it was sent.
      if (canSend) onRetry(reworded === original ? null : reworded);
    };
    const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
      if (event.key === "Escape") setEditing(null);
      if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
        event.preventDefault();
        send();
      }
    };
    return (
      <div role="group" aria-label="Message Claude declined">
        <form
          aria-label="Edit your message"
          className="ml-auto w-full max-w-[min(85%,560px)] rounded-xl border border-border-strong bg-surface px-4 pt-3 pb-2.5"
          onSubmit={(event) => {
            event.preventDefault();
            send();
          }}
        >
          <textarea
            ref={field}
            aria-label="Your message"
            value={editing}
            onChange={(event) => {
              setEditing(event.target.value);
            }}
            onKeyDown={onKeyDown}
            rows={1}
            className="scrollbar-quiet block w-full resize-none bg-transparent text-[16px] leading-6 text-ink outline-none"
          />
          <div className="mt-2 flex justify-end gap-2">
            <Button
              size="sm"
              onClick={() => {
                setEditing(null);
              }}
            >
              Cancel
            </Button>
            <Button variant="primary" size="sm" type="submit" disabled={!canSend}>
              Send
            </Button>
          </div>
        </form>
        {failure}
      </div>
    );
  }

  return (
    <div role="group" aria-label="Message Claude declined">
      {texts.map((text, index) => (
        // Messages sent together, in order; nothing reorders them.
        <div key={index} className={cx(index > 0 && "mt-8")}>
          <UserMessage text={text} />
        </div>
      ))}
      <div className="mt-2 flex flex-wrap items-center justify-end gap-x-0.5 gap-y-1">
        <p className="mr-1.5 text-[13px] leading-5 text-secondary">{note}</p>
        <Button
          variant="ghost"
          size="sm"
          icon={<RotateCcw size={14} strokeWidth={1.75} />}
          disabled={busy || blocked !== null}
          title={blocked ?? undefined}
          onClick={() => {
            onRetry(null);
          }}
        >
          Retry
        </Button>
        <Button
          variant="ghost"
          size="sm"
          icon={<Pencil size={14} strokeWidth={1.75} />}
          disabled={busy}
          onClick={() => {
            setEditing(original);
          }}
        >
          Edit
        </Button>
      </div>
      {failure}
    </div>
  );
}
