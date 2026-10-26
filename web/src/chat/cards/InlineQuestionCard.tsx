import { MessageCircleQuestion } from "lucide-react";
import { useId, useState } from "react";

import { ScriptedNote } from "../../demo/ScriptedNote";
import { Button } from "../../shell/ui/Button";
import { OrgAvatar, Pill } from "../parts";
import type { InlineQuestionItem } from "../types";

export interface InlineQuestionCardProps {
  item: InlineQuestionItem;
  busy: boolean;
  error: string | null;
  onAnswer: (text: string) => void;
}

/** How a settled item reads: what the member did, and what the words under it are. */
const SETTLED = {
  approve: { pill: "Answered", tone: "success", caption: null },
  decline: { pill: "Declined", tone: "muted", caption: "Your note for the representative" },
  take_over: { pill: "You replied directly", tone: "success", caption: "What you sent" },
} as const;

/**
 * A consult the member's own representative routed to them, in the chat the request
 * came from: a counterparty's question the brief doesn't answer, or terms to agree to.
 * The answer is released to that counterparty's conversation only.
 */
export function InlineQuestionCard({ item, busy, error, onAnswer }: InlineQuestionCardProps) {
  const [answer, setAnswer] = useState("");
  const titleId = useId();
  const open = item.status === "open";
  const settled = item.status === "resolved" && item.action !== null ? SETTLED[item.action] : null;
  const submit = () => {
    const text = answer.trim();
    if (text !== "" && !busy) onAnswer(text);
  };

  return (
    <section
      aria-labelledby={titleId}
      className="rounded-menu border border-border bg-surface shadow-card"
    >
      <header className="flex items-center gap-2.5 px-4 pt-3.5">
        <OrgAvatar org={item.from_org} size={22} />
        <p id={titleId} className="min-w-0 flex-1 truncate text-[13.5px] text-muted">
          Your representative asks about <span className="text-secondary">{item.from_org.name}</span>
        </p>
        {open ? (
          <Pill tone="accent" icon={<MessageCircleQuestion size={12} strokeWidth={2} />}>
            Needs your answer
          </Pill>
        ) : settled !== null ? (
          <Pill tone={settled.tone}>{settled.pill}</Pill>
        ) : (
          <Pill tone="muted">No longer needed</Pill>
        )}
      </header>
      <p className="px-4 pt-2.5 font-serif text-[16.5px] leading-[1.5] text-ink">{item.question}</p>
      {open ? (
        <div className="px-4 pt-3 pb-4">
          <textarea
            rows={2}
            value={answer}
            disabled={busy}
            placeholder="Your answer…"
            aria-label={`Your answer about ${item.from_org.name}`}
            onChange={(event) => {
              setAnswer(event.target.value);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                event.preventDefault();
                submit();
              }
            }}
            className="block min-h-[64px] w-full resize-y rounded-control border border-border-strong bg-surface px-3 py-2 text-[14.5px] leading-[1.5] text-ink outline-none transition-colors placeholder:text-faint focus:border-accent"
          />
          <div className="mt-2.5 flex items-center gap-3">
            <p className="min-w-0 flex-1 text-[12.5px] leading-5 text-muted">
              Your representative can share this with {item.from_org.name}, in this conversation only.
            </p>
            <Button variant="primary" disabled={busy || answer.trim() === ""} onClick={submit}>
              {busy ? "Sending…" : "Send answer"}
            </Button>
          </div>
          <ScriptedNote className="mt-2" />
          {error !== null && (
            <p role="alert" className="mt-2 text-[13px] text-danger">
              {error}
            </p>
          )}
        </div>
      ) : (
        <div className="px-4 pt-2.5 pb-4">
          {settled?.caption != null && item.answer !== null && (
            <p className="pb-1 text-[12.5px] text-muted">{settled.caption}</p>
          )}
          {item.answer !== null && (
            <p className="rounded-control bg-bubble px-3 py-2 text-[14px] leading-[1.5] whitespace-pre-wrap text-secondary">
              {item.answer}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
