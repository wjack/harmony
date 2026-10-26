import { CircleAlert, TriangleAlert } from "lucide-react";

import { Button, cx } from "../shell/ui";

interface SaveBarProps {
  /** Why the draft can't be saved as typed, or why the last save failed. */
  problem: string | null;
  /** Something to know before saving (another admin's change), short of a failure. */
  notice: string | null;
  saving: boolean;
  /** The draft is invalid as typed: nothing to send until it's fixed. */
  blocked: boolean;
  /** Whether live threads hear about the change: the profile, instructions or what you share. */
  notifiesLive: boolean;
  onCancel: () => void;
  onSave: () => void;
  /** Drops the edits another admin's change clashes with; shown only when there are some. */
  onUseTheirs?: () => void;
}

/** Settings' Cancel and black "Save changes", pinned to the foot of the pane while there are edits. */
export function SaveBar({
  problem,
  notice,
  saving,
  blocked,
  notifiesLive,
  onCancel,
  onSave,
  onUseTheirs,
}: SaveBarProps) {
  const tone = problem !== null ? "danger" : notice !== null ? "warning" : null;
  const Icon = tone === "danger" ? CircleAlert : TriangleAlert;
  return (
    <div className="sticky bottom-0 z-10 border-t border-border bg-canvas/95 backdrop-blur-[6px]">
      {/* A long notice takes its own line rather than squeezing into a word-wide column. */}
      <div className="flex max-w-[808px] flex-wrap items-center gap-x-3 gap-y-2 px-6 py-3">
        <p
          role={tone !== null ? "alert" : undefined}
          className={cx(
            "flex min-w-0 grow basis-64 items-start gap-1.5 text-[13px] leading-5",
            tone === "danger" ? "text-danger" : tone === "warning" ? "text-warning" : "text-muted",
          )}
        >
          {tone !== null && <Icon size={15} strokeWidth={1.75} className="mt-0.5 shrink-0" aria-hidden />}
          <span>
            {problem ??
              notice ??
              (notifiesLive
                ? "Unsaved changes. Live threads will get a notice."
                : "Unsaved changes. They apply at the next decision.")}
          </span>
        </p>
        <div className="ml-auto flex shrink-0 gap-3">
          <Button onClick={onCancel} disabled={saving}>
            Cancel
          </Button>
          {onUseTheirs !== undefined && (
            <Button onClick={onUseTheirs} disabled={saving}>
              Use theirs
            </Button>
          )}
          <Button variant="primary" onClick={onSave} disabled={saving || blocked}>
            {saving ? "Saving…" : onUseTheirs !== undefined ? "Save anyway" : "Save changes"}
          </Button>
        </div>
      </div>
    </div>
  );
}
