import { CircleX } from "lucide-react";
import { useState, type ReactNode } from "react";

import { useAutoHeight } from "../requests/autoHeight";
import { Button } from "../shell/ui";
import { FootShell } from "./FootShell";

export interface DeclineStepProps {
  /** A reason is needed before Decline (the request view's items, not outreach). */
  required: boolean;
  /** Who sees the reason: "Claude may pass this reason on to Henderson Frame Lab." */
  caption: ReactNode;
  /** The decline is on its way: the step is locked. */
  pending?: boolean;
  onCancel: () => void;
  /** The trimmed reason, or null when an optional reason was left empty. */
  onConfirm: (reason: string | null) => void;
  /** The step's heading: "Decline to answer", "Don't send this request". */
  title?: string;
}

/**
 * The small Decline step that takes the card foot's place. Esc cancels; ⌘↵ / Ctrl↵ in
 * the reason declines (and never reaches the card's approve shortcut).
 */
export function DeclineStep({
  required,
  caption,
  pending = false,
  onCancel,
  onConfirm,
  title = "Decline",
}: DeclineStepProps) {
  const [reason, setReason] = useState("");
  const field = useAutoHeight(reason);
  const trimmed = reason.trim();
  const ready = !pending && (!required || trimmed !== "");
  const confirm = () => {
    if (ready) onConfirm(trimmed === "" ? null : trimmed);
  };
  return (
    <FootShell>
      <section aria-label={title}>
        <h3 className="flex items-center gap-1.5 text-ui-sm font-medium text-secondary">
          <CircleX size={16} strokeWidth={1.75} aria-hidden />
          {title}
        </h3>
        <textarea
          ref={field}
          aria-label="Reason"
          placeholder={required ? "Your reason" : "Your reason (optional)"}
          rows={2}
          value={reason}
          disabled={pending}
          autoFocus
          onChange={(event) => {
            setReason(event.target.value);
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.stopPropagation();
              onCancel();
            } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              confirm();
            }
          }}
          className="mt-2 block min-h-16 w-full resize-none overflow-hidden rounded-[10px] bg-surface px-2.5 py-2 text-ui text-ink shadow-[0_0_0_1px_var(--border-strong),0_0_0_4px_rgb(20_20_19/0.05)] outline-none placeholder:text-faint"
        />
        <div className="mt-2.5 flex items-center gap-2">
          <p className="min-w-0 flex-1 text-ui-xs text-muted">
            {caption}
            {!required && " You can leave it empty."}
          </p>
          <Button variant="ghost" disabled={pending} onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="danger" disabled={!ready} onClick={confirm}>
            Decline
          </Button>
        </div>
      </section>
    </FootShell>
  );
}
