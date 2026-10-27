import { ArrowUp, AudioLines, ChevronDown } from "lucide-react";
import { useImperativeHandle, useLayoutEffect, useRef, type KeyboardEvent, type Ref } from "react";

import { useSession } from "../app/queries";
import { cx } from "../shell/ui/cx";
import { BudgetStrip } from "./BudgetStrip";
import { budgetLine, useBudgetState } from "./failureCopy";
import { PlusMenu } from "./PlusMenu";
import { SendModeChip, type SendModeProps } from "./SendMode";

const MAX_TEXTAREA_PX = 320;

/** What a parent can do to the composer: put the cursor in it. */
export interface ComposerHandle {
  focus: () => void;
}

export interface ComposerProps extends SendModeProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: (text: string) => void;
  placeholder: string;
  /** The home composer is taller, as in Claude's new-chat screen. */
  variant?: "home" | "reply";
  /** A message is on its way: the send button dims and Enter does nothing. */
  sending?: boolean;
  autoFocus?: boolean;
  onUnavailable: (feature: string) => void;
  ref?: Ref<ComposerHandle>;
  className?: string;
}

/**
 * Claude's composer: a white, 20px-radius card with the textarea on top and a row with
 * `+`, the model label and the send button (a voice icon while empty). Enter sends,
 * Shift+Enter starts a new line. While a spend cap stops live turns, a strip on top
 * says which, and nothing can be sent until the budget reads `ok` again (LIVE_DEMO_PLAN
 * 3.5); the draft stays.
 */
export function Composer({
  value,
  onChange,
  onSubmit,
  placeholder,
  variant = "reply",
  sending = false,
  autoFocus = false,
  onUnavailable,
  sendMode,
  onSendModeChange,
  ref,
  className,
}: ComposerProps) {
  const ownRef = useRef<HTMLTextAreaElement | null>(null);
  const model = useSession().data?.internal_model;
  const blocked = budgetLine(useBudgetState());
  const empty = value.trim() === "";

  useImperativeHandle(ref, () => ({
    focus: () => {
      const textarea = ownRef.current;
      if (textarea === null) return;
      textarea.focus();
      textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    },
  }));

  // Grow with the text up to a cap, then scroll; the browser can't size a textarea to
  // its content everywhere yet.
  useLayoutEffect(() => {
    const textarea = ownRef.current;
    if (textarea === null) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, MAX_TEXTAREA_PX)}px`;
  }, [value]);

  const submit = () => {
    if (empty || sending || blocked !== null) return;
    onSubmit(value.trim());
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  return (
    <div className={className}>
      {blocked !== null && <BudgetStrip line={blocked} />}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
        className="relative flex flex-col rounded-composer border border-border bg-surface shadow-composer transition-[border-color,box-shadow] focus-within:border-border-strong"
      >
        <textarea
          ref={ownRef}
          rows={1}
          value={value}
          autoFocus={autoFocus}
          disabled={blocked !== null}
          placeholder={placeholder}
          aria-label="Write your message to Claude"
          onChange={(event) => {
            onChange(event.target.value);
          }}
          onKeyDown={onKeyDown}
          className={cx(
            "scrollbar-quiet block w-full resize-none bg-transparent px-[22px] pt-5 pb-2 text-[16px] leading-6 text-ink outline-none placeholder:text-muted disabled:cursor-not-allowed disabled:text-muted",
            variant === "home" ? "min-h-[72px]" : "min-h-[52px]",
          )}
        />
        <div className="flex items-center gap-1 px-4 pt-1 pb-4">
          <PlusMenu onUnavailable={onUnavailable} sendMode={sendMode} onSendModeChange={onSendModeChange} />
          <div className="flex-1" />
          <SendModeChip sendMode={sendMode} onSendModeChange={onSendModeChange} />
          {model !== undefined && (
            <button
              type="button"
              // As it reads; the two tones' spans would otherwise run together in the name.
              aria-label={model.adaptive ? `${model.label} · Adaptive` : model.label}
              onClick={() => {
                onUnavailable("Choosing a model");
              }}
              className="inline-flex h-8 min-w-0 items-center gap-1 rounded-control px-2 text-[14px] text-secondary transition-colors hover:bg-hover"
            >
              <span className="truncate">
                <span className="text-ink/85">{model.label}</span>
                {model.adaptive && <span className="text-muted"> · Adaptive</span>}
              </span>
              <ChevronDown size={14} strokeWidth={1.6} className="shrink-0 text-muted" />
            </button>
          )}
          {empty ? (
            <button
              type="button"
              aria-label="Use voice mode"
              onClick={() => {
                onUnavailable("Voice mode");
              }}
              className="inline-flex size-8 shrink-0 items-center justify-center rounded-control text-secondary transition-colors hover:bg-hover hover:text-ink"
            >
              <AudioLines size={20} strokeWidth={1.5} />
            </button>
          ) : (
            <button
              type="submit"
              aria-label="Send message"
              disabled={sending || blocked !== null}
              className="inline-flex size-8 shrink-0 items-center justify-center rounded-control bg-accent-strong text-white transition-colors hover:brightness-95 disabled:bg-accent-strong/40"
            >
              <ArrowUp size={17} strokeWidth={2.2} />
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
