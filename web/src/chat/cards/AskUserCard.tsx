import { ArrowRight, Pencil, X } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { Button, IconButton } from "../../shell/ui/Button";
import { cx } from "../../shell/ui/cx";

export interface AskOption {
  id: string;
  label: ReactNode;
  /** A muted second line. */
  hint?: ReactNode;
  onSelect: () => void;
}

export interface AskUserCardProps {
  title: ReactNode;
  /** The summary between the title and the options. */
  children?: ReactNode;
  options: readonly AskOption[];
  /** The "Something else" row: free text that answers instead of the options. */
  otherPlaceholder?: string;
  /** Its text, kept by the caller so putting the card away doesn't lose it. */
  other: string;
  onOtherChange: (text: string) => void;
  onOther: (text: string) => void;
  /** ×, Esc and Skip: put the card away and use the composer. */
  onDismiss: () => void;
  busy?: boolean;
  error?: string | null;
  /** Replaces the option list (a sub-step such as picking recipients). */
  panel?: ReactNode;
  /** Esc in the panel: back to the options. */
  onBack?: () => void;
  /**
   * The card appeared on its own while the member was on the page, rather than when
   * they opened it: options don't respond for a moment, so keystrokes and clicks meant
   * for the composer it replaced can't answer it.
   */
  arriving?: boolean;
  labelledBy: string;
}

/** How long an arriving card ignores picks. */
export const ARRIVAL_PAUSE_MS = 600;

const FOCUSABLE = "input, textarea, select, button, [tabindex]:not([tabindex='-1'])";

/**
 * Claude's ask-user card, drawn where the composer is: a serif question, numbered
 * options (↑↓ to move, Enter or the digit to pick), and a last "Something else" row
 * whose text answers in place of the options.
 */
export function AskUserCard({
  title,
  children,
  options,
  otherPlaceholder = "Something else",
  other,
  onOtherChange,
  onOther,
  onDismiss,
  busy = false,
  error = null,
  panel,
  onBack,
  arriving = false,
  labelledBy,
}: AskUserCardProps) {
  const [focused, setFocused] = useState(0);
  const [armed, setArmed] = useState(!arriving);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const otherRef = useRef<HTMLInputElement | null>(null);
  /** Which view (options or panel) focus was last placed for. */
  const placedFor = useRef<boolean | null>(null);
  const otherIndex = options.length;
  const inPanel = panel !== undefined;

  useEffect(() => {
    if (armed) return;
    const timer = setTimeout(() => {
      setArmed(true);
    }, ARRIVAL_PAUSE_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [armed]);

  // Claude moves focus to the question so the arrow keys work straight away, and into
  // a panel as it opens. On arrival it leaves focus alone if the member is typing
  // somewhere else on the page (an inline answer, say); the composer it replaces is
  // already gone by now.
  useEffect(() => {
    const root = rootRef.current;
    if (root === null || placedFor.current === inPanel) return;
    const first = placedFor.current === null;
    placedFor.current = inPanel;
    const active = document.activeElement;
    if (first && active !== null && active !== document.body && !root.contains(active)) return;
    const target = inPanel ? panelRef.current?.querySelector<HTMLElement>(FOCUSABLE) : listRef.current;
    (target ?? root).focus({ preventScroll: true });
  }, [inPanel]);

  const optionId = (index: number) => `${labelledBy}-option-${index}`;

  const pick = (index: number) => {
    if (busy || !armed) return;
    if (index === otherIndex) {
      otherRef.current?.focus();
      return;
    }
    options[index]?.onSelect();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      if (inPanel && onBack !== undefined) onBack();
      else onDismiss();
      return;
    }
    if (inPanel) return;
    const inList = event.target === listRef.current;
    const inOther = event.target === otherRef.current;
    // The ×, Skip and Send buttons keep their own Enter and Space.
    if (!inList && !inOther) return;
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        setFocused((index) => Math.min(index + 1, otherIndex));
        if (focused + 1 >= otherIndex) otherRef.current?.focus();
        return;
      case "ArrowUp":
        event.preventDefault();
        setFocused((index) => Math.max(index - 1, 0));
        if (inOther) listRef.current?.focus();
        return;
      case "Enter":
        if (!inList) return;
        event.preventDefault();
        pick(focused);
        return;
    }
    const digit = Number(event.key);
    if (inList && Number.isInteger(digit) && digit >= 1 && digit <= options.length) {
      event.preventDefault();
      setFocused(digit - 1);
      pick(digit - 1);
    }
  };

  const submitOther = () => {
    const text = other.trim();
    if (text === "" || busy) return;
    onOther(text);
  };

  const divider = (index: number) => (
    <div
      aria-hidden
      className={cx("mx-3 h-px", focused === index || focused === index - 1 ? "bg-transparent" : "bg-border/70")}
    />
  );

  return (
    <div>
      <div
        ref={rootRef}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className="relative rounded-composer border border-border bg-surface shadow-composer outline-none animate-pop-in"
      >
        <div className="flex items-start gap-3 pt-4 pr-3 pb-3.5 pl-5">
          <h2 id={labelledBy} className="min-w-0 flex-1 pt-1 font-serif text-[18px] leading-6 text-ink">
            {title}
          </h2>
          <IconButton label="Skip for now" onClick={onDismiss}>
            <X size={18} strokeWidth={1.5} />
          </IconButton>
        </div>
        {children !== undefined && <div className="px-5 pb-3">{children}</div>}
        {inPanel ? (
          <div ref={panelRef}>{panel}</div>
        ) : (
          <div
            ref={listRef}
            role="listbox"
            tabIndex={0}
            aria-labelledby={labelledBy}
            aria-activedescendant={focused < otherIndex ? optionId(focused) : undefined}
            className="px-2 pb-2 outline-none"
          >
            {options.map((option, index) => (
              <div key={option.id}>
                {index > 0 && divider(index)}
                <div
                  id={optionId(index)}
                  role="option"
                  aria-selected={focused === index}
                  aria-disabled={busy || !armed}
                  onMouseEnter={() => {
                    setFocused(index);
                  }}
                  onClick={() => {
                    setFocused(index);
                    pick(index);
                  }}
                  className={cx(
                    "flex cursor-pointer items-center gap-3 rounded-xl px-3 py-3 transition-colors",
                    focused === index && "bg-hover/70",
                    busy && "cursor-default opacity-60",
                  )}
                >
                  <span
                    className={cx(
                      "flex size-[30px] shrink-0 items-center justify-center rounded-lg text-[14px] text-secondary tabular-nums",
                      focused === index ? "bg-pressed" : "bg-hover",
                    )}
                  >
                    {index + 1}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-[15.5px] leading-5 text-ink">{option.label}</span>
                    {option.hint !== undefined && (
                      <span className="mt-0.5 block truncate text-[13px] leading-5 text-muted">{option.hint}</span>
                    )}
                  </span>
                  {focused === index && <ArrowRight size={16} strokeWidth={1.6} className="shrink-0 text-muted" />}
                </div>
              </div>
            ))}
            {divider(otherIndex)}
            <div
              className={cx(
                "flex items-center gap-3 rounded-xl px-3 py-2.5 transition-colors",
                focused === otherIndex && "bg-hover/70",
              )}
              onMouseEnter={() => {
                setFocused(otherIndex);
              }}
            >
              <span
                className={cx(
                  "flex size-[30px] shrink-0 items-center justify-center rounded-lg text-secondary",
                  focused === otherIndex ? "bg-pressed" : "bg-hover",
                )}
              >
                <Pencil size={14} strokeWidth={1.6} />
              </span>
              <input
                ref={otherRef}
                value={other}
                disabled={busy}
                placeholder={otherPlaceholder}
                aria-label={otherPlaceholder}
                onFocus={() => {
                  setFocused(otherIndex);
                }}
                onChange={(event) => {
                  onOtherChange(event.target.value);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    submitOther();
                  }
                }}
                className="min-w-0 flex-1 bg-transparent text-[15px] leading-5 text-ink outline-none placeholder:text-faint"
              />
              {other.trim() === "" ? (
                <Button size="md" onClick={onDismiss}>
                  Skip
                </Button>
              ) : (
                <Button size="md" variant="primary" disabled={busy} onClick={submitOther}>
                  Send
                </Button>
              )}
            </div>
          </div>
        )}
        {error !== null && (
          <p role="alert" className="px-5 pb-3 text-[13px] text-danger">
            {error}
          </p>
        )}
      </div>
      <p className="mt-2 text-center text-[12px] text-muted">
        {!inPanel ? (
          <>
            ↑↓ to navigate <span className="px-1.5">·</span> Enter to select <span className="px-1.5">·</span> Esc
            to skip
          </>
        ) : onBack !== undefined ? (
          <>Esc to go back</>
        ) : (
          <>Esc to skip</>
        )}
      </p>
    </div>
  );
}
