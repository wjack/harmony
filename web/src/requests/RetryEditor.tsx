import { useEffect, useState, type KeyboardEvent } from "react";

import { Button } from "../shell/ui";
import { useAutoHeight } from "./autoHeight";

/** The field grows with the text up to this height, then scrolls. */
const MAX_FIELD_PX = 240;

interface RetryEditorProps {
  /** The form's and the field's accessible name. */
  label: string;
  /** What the field opens with: the input Claude declined, as it stands. */
  initial: string;
  /** The send button's text. */
  submitLabel: string;
  disabled: boolean;
  onCancel: () => void;
  onSubmit: (text: string) => void;
}

/**
 * A box to reword what Claude declined before it tries again, under the note that says
 * so: Enter or the button sends, Shift+Enter breaks the line, Esc cancels. Opens with the
 * cursor after the text.
 */
export function RetryEditor({ label, initial, submitLabel, disabled, onCancel, onSubmit }: RetryEditorProps) {
  const [text, setText] = useState(initial);
  const field = useAutoHeight(text, MAX_FIELD_PX);
  const trimmed = text.trim();
  const canSend = trimmed !== "" && !disabled;

  useEffect(() => {
    const element = field.current;
    if (element === null) return;
    element.focus();
    element.setSelectionRange(element.value.length, element.value.length);
  }, [field]);

  const send = () => {
    if (canSend) onSubmit(trimmed);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Escape") {
      // The card's own Escape handling never sees it.
      event.stopPropagation();
      onCancel();
    }
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send();
    }
  };

  return (
    <form
      aria-label={label}
      className="mt-1 rounded-control border border-border-strong bg-surface px-3 pt-2.5 pb-2"
      onSubmit={(event) => {
        event.preventDefault();
        send();
      }}
    >
      <textarea
        ref={field}
        aria-label={label}
        value={text}
        onChange={(event) => {
          setText(event.target.value);
        }}
        onKeyDown={onKeyDown}
        rows={1}
        className="scrollbar-quiet block w-full resize-none bg-transparent text-ui leading-[22px] text-ink outline-none"
      />
      <div className="mt-2 flex justify-end gap-2">
        <Button size="sm" onClick={onCancel}>
          Cancel
        </Button>
        <Button variant="primary" size="sm" type="submit" disabled={!canSend}>
          {submitLabel}
        </Button>
      </div>
    </form>
  );
}
