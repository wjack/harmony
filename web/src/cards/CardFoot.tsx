import type { ReactNode } from "react";

import { Button } from "../shell/ui";
import { FootShell } from "./FootShell";

export interface CardFootProps {
  onDecline: () => void;
  onApprove: () => void;
  /** "Approve and send" unless the card says otherwise. */
  approveLabel?: string;
  approveDisabled?: boolean;
  declineDisabled?: boolean;
  /** Context, on the left of the hint row: a mode line, or the unsent-Edit `CardHint`. */
  hintLeft?: ReactNode;
  /** What approving does, on the right: "Claude replies to Henderson Lab with this answer". */
  hintRight?: ReactNode;
}

/** Decline, quiet on the left; Approve and send on the right; a hint row under them. */
export function CardFoot({
  onDecline,
  onApprove,
  approveLabel = "Approve and send",
  approveDisabled = false,
  declineDisabled = false,
  hintLeft,
  hintRight,
}: CardFootProps) {
  return (
    <FootShell>
      <div className="flex items-center gap-2">
        <Button variant="ghost" disabled={declineDisabled} onClick={onDecline}>
          Decline
        </Button>
        <span className="flex-1" />
        <Button variant="primary" disabled={approveDisabled} onClick={onApprove}>
          {approveLabel}
        </Button>
      </div>
      {(hintLeft !== undefined || hintRight !== undefined) && (
        // Wraps as two whole lines when both sides don't fit, rather than squeezing each
        // into a ragged column (an unsent Edit beside a long org name at 1000 px).
        <div className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-ui-xs text-muted">
          <div className="min-w-0">{hintLeft}</div>
          <div className="ml-auto text-right">{hintRight}</div>
        </div>
      )}
    </FootShell>
  );
}
