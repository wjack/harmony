import { useId, useLayoutEffect, useRef, useState, type TextareaHTMLAttributes } from "react";

import { cx } from "../../shell/ui/cx";

/** When the stylesheet gives no line height (tests), the 14px text's 1.55 leading. */
const FALLBACK_LINE_PX = 22;

interface ExpandingTextareaProps
  extends Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "value" | "onChange" | "rows" | "style"> {
  value: string;
  onChange: (value: string) => void;
  /** At most this many lines while collapsed; past them, "View more". */
  maxRows: number;
  /** Never shorter than this many lines, so an empty field still reads as one. */
  minRows?: number;
}

/**
 * A textarea sized to its text, up to `maxRows` lines; longer text gets "View more",
 * which grows it to the whole text, with no scroll inside it (LIVE_DEMO_PLAN 3.11).
 * A `rows` count can't do this: it fixes the height, so the text would scroll.
 */
export function ExpandingTextarea({
  value,
  onChange,
  maxRows,
  minRows = 2,
  id,
  className,
  ...props
}: ExpandingTextareaProps) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const ownId = useId();
  const textareaId = id ?? ownId;
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);

  // Measured at height 0, so `scrollHeight` is the text's own height, not the box's.
  useLayoutEffect(() => {
    const textarea = ref.current;
    if (textarea === null) return;
    const style = getComputedStyle(textarea);
    const line = parseFloat(style.lineHeight) || FALLBACK_LINE_PX;
    const padding = (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);
    const borders = textarea.offsetHeight - textarea.clientHeight;
    textarea.style.height = "0px";
    const text = Math.max(textarea.scrollHeight, minRows * line + padding);
    const cap = maxRows * line + padding;
    setOverflows(text > cap + 1);
    textarea.style.height = `${(expanded ? text : Math.min(text, cap)) + borders}px`;
  }, [value, expanded, maxRows, minRows]);

  return (
    <div>
      <textarea
        ref={ref}
        id={textareaId}
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
        }}
        className={cx(
          "block w-full resize-none rounded-control border border-border-strong bg-surface px-3 py-2 text-[14px] leading-[1.55] text-ink outline-none transition-colors placeholder:text-faint focus:border-accent",
          // Collapsed, the rest of a long text scrolls under the cut, as any textarea's would.
          !expanded && overflows ? "scrollbar-quiet overflow-y-auto" : "overflow-hidden",
          className,
        )}
        {...props}
      />
      {(overflows || expanded) && (
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={textareaId}
          onClick={() => {
            setExpanded((current) => !current);
          }}
          className="mt-1 text-[12.5px] font-medium text-secondary underline decoration-border-strong underline-offset-2 hover:text-ink"
        >
          {expanded ? "View less" : "View more"}
        </button>
      )}
    </div>
  );
}
