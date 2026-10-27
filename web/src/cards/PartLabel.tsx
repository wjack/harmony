import { ChevronLeft, ChevronRight, Undo2 } from "lucide-react";

import { IconButton } from "../shell/ui";

export interface PartLabelProps {
  /** The part's name: "Draft reply", "Brief", "Ask". */
  label: string;
  /** A muted note after the name: "· Claude works from this in every conversation". */
  caption?: string;
  /**
   * Claude rewrote the part: a pager through its versions. `index` is 0-based; the
   * first version is Claude's original, so only a later one reads "rewritten by Claude".
   */
  rewritten?: { index: number; count: number; onPrev: () => void; onNext: () => void };
  /** The member changed Claude's wording. */
  edited?: { onUndo: () => void };
}

/**
 * A card part's label, with its caption muted: "{label} · rewritten by Claude ‹ 2 of 2 ›",
 * "{label} · edited by you" with Undo my edits, or both, with Undo my edits before the pager.
 */
export function PartLabel({ label, caption, rewritten, edited }: PartLabelProps) {
  const pager = rewritten !== undefined && rewritten.count > 1;
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1 text-ui-sm text-muted">
      <span className="text-secondary">{label}</span>
      {caption !== undefined && <span>· {caption}</span>}
      {rewritten !== undefined && rewritten.index > 0 && (
        <span className="whitespace-nowrap text-link">· rewritten by Claude</span>
      )}
      {edited && <span className="whitespace-nowrap text-secondary">· edited by you</span>}
      {(edited !== undefined || pager) && (
        <span className="ml-auto inline-flex items-center gap-3">
          {edited && (
            <button
              type="button"
              onClick={edited.onUndo}
              className="inline-flex items-center gap-1 whitespace-nowrap text-secondary hover:text-ink"
            >
              <Undo2 size={14} strokeWidth={1.75} aria-hidden />
              Undo my edits
            </button>
          )}
          {pager && <Pager {...rewritten} />}
        </span>
      )}
    </div>
  );
}

function Pager({ index, count, onPrev, onNext }: NonNullable<PartLabelProps["rewritten"]>) {
  return (
    <span className="inline-flex items-center gap-0.5 text-ui-xs whitespace-nowrap tabular-nums">
      <IconButton label="Earlier version" size="sm" disabled={index === 0} onClick={onPrev}>
        <ChevronLeft size={15} strokeWidth={1.5} />
      </IconButton>
      <span aria-live="polite">
        {index + 1} of {count}
      </span>
      <IconButton label="Later version" size="sm" disabled={index === count - 1} onClick={onNext}>
        <ChevronRight size={15} strokeWidth={1.5} />
      </IconButton>
    </span>
  );
}
