import { CircleAlert } from "lucide-react";

/**
 * Why live turns can't start (`budgetLine`), attached to the top edge of whatever acts
 * through them, like Claude's usage-limit strip: the card waiting on the member (the
 * chat's outreach card, the request view's decision card), else the composer. One shows
 * at a time. The element under it must be positioned so it overlaps the strip's padded
 * bottom.
 */
export function BudgetStrip({ line }: { line: string }) {
  return (
    <p
      role="status"
      className="mx-2.5 -mb-3 flex items-center gap-2 rounded-t-[14px] bg-bubble pt-2 pr-3 pb-5 pl-4 text-[14px] text-secondary"
    >
      <CircleAlert size={15} strokeWidth={1.7} className="shrink-0 text-warning" aria-hidden />
      <span className="min-w-0 flex-1">{line}</span>
    </p>
  );
}
