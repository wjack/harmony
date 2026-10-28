import { Code, Coffee, GraduationCap, Lightbulb, PenLine } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { firstName } from "../shell/names";
import { Spark } from "../shell/ui/Spark";
import { Composer, type ComposerHandle } from "./Composer";
import { greeting } from "./format";
import type { SendModeProps } from "./SendMode";

const STARTERS = [
  { label: "Write", Icon: PenLine },
  { label: "Learn", Icon: GraduationCap },
  { label: "Code", Icon: Code },
  { label: "Life stuff", Icon: Coffee },
  { label: "Claude's choice", Icon: Lightbulb },
] as const;

const CHIP =
  "inline-flex h-8 items-center gap-2 rounded-control border border-border bg-canvas px-2.5 text-[15px] text-secondary transition-colors hover:border-border-strong hover:bg-hover hover:text-ink";

export interface NewChatProps extends SendModeProps {
  /** The acting member's display name ("Alex Chen"). */
  memberName: string | null;
  /** The walkthrough's suggested opening message, if the story has one. */
  suggestion: string | null;
  /** Put in the composer once, with the cursor after it; then `onPrefilled` says it's taken. */
  prefill?: string | null;
  onPrefilled?: () => void;
  sending: boolean;
  error: string | null;
  /** The organization's default didn't load, so a chat that follows it can't start: load it again. */
  onRetrySendMode?: (() => void) | null;
  onSend: (text: string) => void;
  onUnavailable: (feature: string) => void;
  /** For the gallery: a fixed hour instead of the clock's. */
  hour?: number;
}

/**
 * Claude's new-chat screen: the spark and a serif greeting, the composer, and the
 * starter chips. Harmony adds the walkthrough's suggested message as one more chip: a
 * click fills the composer, and the visitor can edit or ignore it. A first visit fills
 * it once by itself (`prefill`).
 */
export function NewChat({
  memberName,
  suggestion,
  prefill = null,
  onPrefilled,
  sending,
  error,
  onRetrySendMode = null,
  onSend,
  onUnavailable,
  sendMode,
  onSendModeChange,
  hour,
}: NewChatProps) {
  const [draft, setDraft] = useState("");
  const [prefilled, setPrefilled] = useState(false);
  const composerRef = useRef<ComposerHandle>(null);
  const salutation = greeting(hour ?? new Date().getHours());

  if (prefill !== null && !prefilled) {
    setPrefilled(true);
    setDraft(prefill);
  }
  useEffect(() => {
    if (!prefilled) return;
    composerRef.current?.focus();
    onPrefilled?.();
  }, [prefilled, onPrefilled]);

  return (
    <div className="flex h-full min-h-0 flex-col items-center overflow-y-auto px-4">
      <div className="flex w-full max-w-[680px] flex-col items-center pt-[max(48px,24vh)] pb-16">
        <h1 className="flex items-center gap-3 text-center font-serif text-[44px] leading-[1.15] font-normal tracking-[-0.015em] text-secondary">
          <Spark size={36} />
          <span>{memberName === null ? salutation : `${salutation}, ${firstName(memberName)}`}</span>
        </h1>
        <Composer
          className="mt-9 w-full"
          variant="home"
          value={draft}
          onChange={setDraft}
          onSubmit={onSend}
          sending={sending}
          autoFocus
          placeholder="How can I help you today?"
          onUnavailable={onUnavailable}
          sendMode={sendMode}
          onSendModeChange={onSendModeChange}
          ref={composerRef}
        />
        {error !== null && (
          <p role="alert" className="mt-3 text-[13px] text-danger">
            {error}
          </p>
        )}
        {onRetrySendMode !== null && (
          <p role="alert" className="mt-3 text-center text-[13px] text-danger">
            Couldn&apos;t load how your organization sends requests, so this chat can&apos;t start yet.{" "}
            <button
              type="button"
              onClick={onRetrySendMode}
              className="font-medium underline decoration-danger/40 underline-offset-[3px] hover:decoration-danger"
            >
              Try again
            </button>
          </p>
        )}
        <div className="mt-4 flex flex-wrap justify-center gap-2">
          {STARTERS.map(({ label, Icon }) => (
            <button
              key={label}
              type="button"
              className={CHIP}
              onClick={() => {
                onUnavailable(`Starter prompts (${label})`);
              }}
            >
              <Icon size={16} strokeWidth={1.5} />
              {label}
            </button>
          ))}
        </div>
        {suggestion !== null && (
          <div className="mt-7 flex w-full max-w-full flex-col items-center gap-2">
            <span className="text-[12px] text-muted">Suggested</span>
            <button
              type="button"
              title={suggestion}
              className={`${CHIP} max-w-full`}
              onClick={() => {
                setDraft(suggestion);
                composerRef.current?.focus();
              }}
            >
              <Spark size={15} />
              <span className="truncate">{suggestion}</span>
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
