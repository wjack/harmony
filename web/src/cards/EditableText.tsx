import { useAutoHeight } from "../requests/autoHeight";
import { cx } from "../shell/ui";

export interface EditableTextProps {
  value: string;
  /** Claude's text. Once `value` differs from it, the field keeps its outline. */
  original: string;
  onChange: (value: string) => void;
  /** The field's accessible name ("Brief", "Draft reply"). */
  label: string;
  disabled?: boolean;
  autoFocus?: boolean;
  /** The server's limit, so an over-long edit stops at the field rather than on submit. */
  maxLength?: number;
}

/**
 * Text edited where it stands: a click puts the caret in it, with no Edit button. It
 * grows with its text and never scrolls inside. Esc ends editing and keeps the edits.
 * The ring is neutral, never blue: blue marks what Claude did.
 */
export function EditableText({
  value,
  original,
  onChange,
  label,
  disabled = false,
  autoFocus = false,
  maxLength,
}: EditableTextProps) {
  const field = useAutoHeight(value);
  const edited = value !== original;
  return (
    <textarea
      ref={field}
      aria-label={label}
      data-edited={edited || undefined}
      rows={1}
      value={value}
      readOnly={disabled}
      autoFocus={autoFocus}
      maxLength={maxLength}
      onChange={(event) => {
        onChange(event.target.value);
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          event.currentTarget.blur();
        }
      }}
      className={cx(
        "-mx-2 block w-[calc(100%+1rem)] resize-none overflow-hidden rounded-control bg-transparent px-2 py-1 font-serif text-ui-md break-words whitespace-pre-wrap text-ink outline-none",
        !disabled &&
          "cursor-text focus:bg-surface focus:shadow-[0_0_0_1px_var(--border-strong),0_0_0_4px_rgb(20_20_19/0.05)]",
        edited && "bg-surface shadow-[0_0_0_1px_var(--border-strong),0_0_0_4px_rgb(20_20_19/0.05)]",
      )}
    />
  );
}
